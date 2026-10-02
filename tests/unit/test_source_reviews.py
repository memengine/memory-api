from __future__ import annotations

import copy
import json
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from api.db.models import PendingExtractionCandidate, ProxyUser
from api.errors import APIError
from api.routers.memories import answer_source_review
from api.schemas.extraction_schemas import PendingExtractedMemory
from api.schemas.requests import MemorySourceReviewAnswerRequest
from api.services.source_review_service import (
    MAX_REVIEWS,
    intent_for,
    list_source_reviews,
    review_intent,
    review_version,
    view_review,
)
from api.tasks.extraction_tasks import _can_reinforce_candidate, _candidate_fingerprint
from tests.unit.test_conflict_resolver import (
    FakeSession,
    make_existing_memory,
    source_candidate,
    source_response,
)
from tests.unit.test_tenant_clarification_answer import (
    _ProxyUserService,
    _request,
    _Session,
)


def candidate_for(target=None, *, evidence=None):
    now = datetime.now(UTC)
    return PendingExtractionCandidate(
        id=uuid.uuid4(), tenant_id=uuid.uuid4(),
        proxy_user_id=target.proxy_user_id if target else uuid.uuid4(),
        content="Ruby could replace Go, but I have not decided.", category="preference",
        candidate_reason="source_decision_pending", status="pending",
        created_at=now, last_seen_at=now,
        metadata_json={"extraction_evidence": evidence or {
            "grounding_mode": "verified_source_spans",
            "source_spans": [{"turn_sha256": "a" * 64}],
            "source_review": review_intent(target),
        }},
    )


@pytest.mark.parametrize("reason", ["no_new_value", "no_relevant_target", "ambiguous_target", "unsupported_value"])
def test_missing_representation_creates_review_intent_not_a_memory(reason):
    from unittest.mock import MagicMock

    from api.services.conflict_resolver import ConflictResolver, SourceMemoryContext

    target = make_existing_memory()
    incoming, messages = source_candidate("Go could replace Python, but I have not chosen.")
    incoming.validated_evidence["claim_state"] = "uncertain_change"
    model = MagicMock()
    payload = source_response(str(target.id), state="tentative", relation="ambiguous")
    payload["candidate_representation"]["reason"] = reason
    model.complete_sync.return_value = SimpleNamespace(content=json.dumps(payload), total_tokens=20)
    proxy = ProxyUser(id=target.proxy_user_id, tenant_id=uuid.uuid4())
    session = FakeSession(target, proxy)
    resolver = ConflictResolver(session=session, qdrant_service=MagicMock(),
        embedder=lambda _: [0.1] * 3, llm_service=model, source_messages=messages)
    stored = resolver.check_and_store([incoming], user_id=str(target.user_id),
        tenant_id=str(proxy.tenant_id), proxy_user_id=str(proxy.id),
        source_context=SourceMemoryContext((target,), complete=True))
    assert stored == [] and not target.is_archived
    evidence = resolver.last_pending_candidates[0].validated_evidence
    assert evidence["source_review"] == review_intent(target)
    assert "source_representation_unavailable" in evidence["source_decision"]["reason_codes"]
    assert len(session.memories) == 1
    assert resolver.last_user_clarifications_queued == 0
    assert model.complete_sync.call_count == 1


def test_unavailable_provider_is_not_a_user_review():
    from unittest.mock import MagicMock

    from api.services.conflict_resolver import ConflictResolver, SourceMemoryContext
    from api.services.llm_service import AllProvidersFailedError

    incoming, messages = source_candidate("My default language could change.")
    incoming.validated_evidence["claim_state"] = "uncertain_change"
    model = MagicMock()
    model.complete_sync.side_effect = AllProvidersFailedError("offline", providers_tried=[], errors=[])
    resolver = ConflictResolver(session=FakeSession(), qdrant_service=MagicMock(),
        embedder=lambda _: [0.1] * 3, llm_service=model, source_messages=messages)
    assert resolver.check_and_store([incoming], user_id=str(uuid.uuid4()),
        source_context=SourceMemoryContext((), complete=True)) == []
    assert "source_review" not in resolver.last_pending_candidates[0].validated_evidence


@pytest.mark.parametrize("action", ["keep_current", "dismiss", "restate"])
@pytest.mark.asyncio
async def test_review_answer_never_activates_pending_text(action):
    target = make_existing_memory()
    candidate = candidate_for(target)
    session = _Session(candidate, target)
    response = await answer_source_review(
        _request(), str(candidate.id), MemorySourceReviewAnswerRequest(
            external_user_id="u1", version=review_version(candidate), action=action,
        ), _ProxyUserService(candidate.proxy_user_id), session, str(candidate.tenant_id),
    )
    assert target.is_archived is False
    assert response.data.resolved is (action != "restate")
    assert response.data.next_step == ("add_memory" if action == "restate" else None)
    assert candidate.status == ("pending" if action == "restate" else "dismissed")
    assert session.commits == (0 if action == "restate" else 1)
    assert all("FOR UPDATE" in str(sql) for sql in session.statements)


@pytest.mark.parametrize("change", ["content", "authority", "archive", "owner", "expiry", "future", "closed", "review_version"])
@pytest.mark.asyncio
async def test_stale_review_cannot_be_answered(change):
    target = make_existing_memory()
    candidate = candidate_for(target)
    version = review_version(candidate)
    if change == "content":
        target.content = "A different current preference"
    elif change == "authority":
        target.metadata_json = {"provenance": {"authority_priority": 90}}
    elif change == "archive":
        target.is_archived = True
    elif change == "owner":
        target.proxy_user_id = uuid.uuid4()
    elif change == "expiry":
        candidate.last_seen_at = datetime.now(UTC) - timedelta(days=8)
    elif change == "future":
        target.effective_from = datetime.now(UTC) + timedelta(days=1)
    elif change == "closed":
        candidate.status = "dismissed"
    else:
        candidate.content = "Different source turn arriving during review"
    session = _Session(candidate, target)
    with pytest.raises(APIError) as raised:
        await answer_source_review(_request(), str(candidate.id), MemorySourceReviewAnswerRequest(
            external_user_id="u1", version=version, action="keep_current",
        ), _ProxyUserService(candidate.proxy_user_id), session, str(candidate.tenant_id))
    assert raised.value.status_code == 409 and session.commits == 0


@pytest.mark.parametrize("known_user", [True, False])
@pytest.mark.asyncio
async def test_review_is_owner_scoped_without_creating_a_user(known_user):
    session = _Session(None)
    proxy_id = uuid.uuid4() if known_user else None
    with pytest.raises(APIError) as raised:
        await answer_source_review(_request(), str(uuid.uuid4()), MemorySourceReviewAnswerRequest(
            external_user_id="foreign", version="a" * 64, action="dismiss",
        ), _ProxyUserService(proxy_id), session, str(uuid.uuid4()))
    assert raised.value.status_code == 404 and session.commits == 0
    if known_user:
        sql = str(session.statements[0])
        assert "tenant_id" in sql and "proxy_user_id" in sql and "FOR UPDATE" in sql
    else:
        assert session.statements == []


@pytest.mark.asyncio
async def test_unbound_review_has_no_keep_current_action():
    candidate = candidate_for()
    view = view_review(candidate, None, now=datetime.now(UTC))
    assert view.target_memory_id is None and view.actions == ["restate", "dismiss"]
    with pytest.raises(APIError) as raised:
        await answer_source_review(_request(), str(candidate.id), MemorySourceReviewAnswerRequest(
            external_user_id="u1", version=view.version, action="keep_current",
        ), _ProxyUserService(candidate.proxy_user_id), _Session(candidate), str(candidate.tenant_id))
    assert raised.value.status_code == 422


def test_only_internal_verified_source_reviews_are_actionable():
    for evidence in ({}, {"source_review": review_intent(None)}, {
        "grounding_mode": "verified_source_spans", "source_review": {"kind": "other"},
    }):
        assert intent_for(candidate_for(evidence=evidence or {"invalid": True})) is None


def test_review_scope_cannot_be_overwritten_by_prose_reinforcement():
    first, second = make_existing_memory(), make_existing_memory()
    old = candidate_for(first)
    incoming = PendingExtractedMemory(content=old.content, category="preference",
        importance_score=6, confidence=0.9, reasoning="review",
        validated_evidence={"source_review": review_intent(second)})
    assert not _can_reinforce_candidate(old, incoming)
    old_candidate = PendingExtractedMemory(content=old.content, category="preference",
        importance_score=6, confidence=0.9, reasoning="review",
        validated_evidence={"source_review": review_intent(first)})
    assert _candidate_fingerprint(old_candidate) != _candidate_fingerprint(incoming)


@pytest.mark.asyncio
async def test_read_is_bounded_redeliverable_and_never_consumes_review():
    target = make_existing_memory()
    target.content = "X" * 4000
    candidate = candidate_for(target)

    class Rows:
        def __init__(self, rows): self.rows = rows
        def scalars(self): return self
        def all(self): return self.rows

    class Session:
        def __init__(self): self.statements = []
        async def execute(self, sql):
            self.statements.append(sql)
            return Rows([candidate] if "pending_extraction_candidates" in str(sql) else [target])

    session = Session()
    first = await list_source_reviews(session, tenant_id=str(candidate.tenant_id), proxy_user_id=str(candidate.proxy_user_id))
    second = await list_source_reviews(session, tenant_id=str(candidate.tenant_id), proxy_user_id=str(candidate.proxy_user_id))
    assert first == second and len(first) == 1 and candidate.status == "pending"
    assert len(first[0].current_memory_content) == 1000
    assert "metadata_json" not in first[0].model_dump()
    assert session.statements[0]._limit_clause.value == MAX_REVIEWS
    assert "tenant_id" in str(session.statements[0]) and "proxy_user_id" in str(session.statements[0])


@pytest.mark.asyncio
async def test_review_answer_requires_write_permission():
    from starlette.responses import Response

    from api.middleware.rate_limiter import RateLimiterMiddleware
    from tests.unit.test_api_key_policy_middleware import _request as policy_request
    async def call_next(_):
        pytest.fail("read-only API key cannot answer a review")
        return Response()
    response = await RateLimiterMiddleware(lambda *_: None).dispatch(
        policy_request(method="POST", path="/v1/memories/source-reviews/id/answer", permissions=("read",)), call_next,
    )
    assert response.status_code == 403


def test_scoped_restatement_updates_only_selected_project_memory():
    """Real resolver with controlled model output; not a semantic accuracy test."""
    from unittest.mock import MagicMock

    from api.db.models import MemoryCategory
    from api.services.conflict_resolver import ConflictResolver, SourceMemoryContext

    project, general = make_existing_memory(), make_existing_memory()
    general.user_id, general.proxy_user_id = project.user_id, project.proxy_user_id
    project.category = general.category = MemoryCategory.preference
    project.content = "For the synthetic Release Check project only, use C++."
    general.content = "My default language for every programming example is C++."
    general_before = (general.content, general.is_archived, copy.deepcopy(general.metadata_json))
    proxy = ProxyUser(id=project.proxy_user_id, tenant_id=uuid.uuid4())
    session = FakeSession(project, proxy)
    session.add(general)
    uncertain, uncertain_messages = source_candidate(
        "For the Release Check project, I'm torn between keeping C++ and switching to Python. I haven't settled on either yet.",
    )
    uncertain.category = "preference"
    uncertain.validated_evidence["claim_state"] = "uncertain_change"
    model = MagicMock()
    model.complete_sync.return_value = SimpleNamespace(content=json.dumps(source_response(
        str(project.id), state="tentative", relation="ambiguous",
    )), total_tokens=20)
    first_resolver = ConflictResolver(session=session, qdrant_service=MagicMock(),
        embedder=lambda _: [0.1] * 3, llm_service=model, source_messages=uncertain_messages)
    assert first_resolver.check_and_store([uncertain], user_id=str(project.user_id),
        tenant_id=str(proxy.tenant_id), proxy_user_id=str(proxy.id),
        source_context=SourceMemoryContext((project, general), complete=True)) == []
    assert not project.is_archived and not general.is_archived
    pending = candidate_for(project, evidence=first_resolver.last_pending_candidates[0].validated_evidence)
    assert view_review(pending, project, now=datetime.now(UTC)) is not None
    text = "I've decided: for the synthetic Release Check project only, Python replaces my earlier C++ default. My preferences outside this project are unchanged."
    incoming, messages = source_candidate(text)
    incoming.category = "preference"
    model = MagicMock()
    model.complete_sync.return_value = SimpleNamespace(content=json.dumps(source_response(
        str(project.id), relation="supersedes",
    )), total_tokens=20)
    resolver = ConflictResolver(session=session, qdrant_service=MagicMock(),
        embedder=lambda _: [0.1] * 3, llm_service=model, source_messages=messages)
    stored = resolver.check_and_store([incoming], user_id=str(project.user_id),
        tenant_id=str(proxy.tenant_id), proxy_user_id=str(proxy.id),
        source_context=SourceMemoryContext((project, general), complete=True))
    assert len(stored) == 1 and project.is_archived
    replacement = session.memories[stored[0].id]
    assert not replacement.is_archived and replacement.previous_version_id == project.id
    assert replacement.content == text
    assert (general.content, general.is_archived, general.metadata_json) == general_before
    assert view_review(pending, project, now=datetime.now(UTC)) is None
    assert "source_review_resolution" not in pending.metadata_json
    prompt = json.loads(model.complete_sync.call_args.kwargs["user_message"])
    assert {row["id"] for row in prompt["existing_candidates"]} == {str(project.id), str(general.id)}


@pytest.mark.asyncio
@pytest.mark.parametrize("rowcount", [0, 1])
async def test_archived_target_expires_review_with_cas_not_user_resolution(rowcount):
    from sqlalchemy.sql.dml import Update

    target = make_existing_memory()
    candidate = candidate_for(target)
    target.is_archived = True

    class Rows:
        def __init__(self, rows): self.rows = rows
        def scalars(self): return self
        def all(self): return self.rows

    class Session:
        def __init__(self): self.statements, self.commits = [], 0
        async def execute(self, statement):
            self.statements.append(statement)
            if isinstance(statement, Update):
                return SimpleNamespace(rowcount=rowcount)
            return Rows([candidate] if "pending_extraction_candidates" in str(statement) else [target])
        async def commit(self): self.commits += 1

    session = Session()
    assert await list_source_reviews(session, tenant_id=str(candidate.tenant_id),
        proxy_user_id=str(candidate.proxy_user_id)) == []
    assert session.commits == rowcount
    update = session.statements[-1]
    values = update.compile().params
    assert values["status"] == "expired"
    for field in ("tenant_id", "proxy_user_id", "status", "content", "last_seen_at", "metadata"):
        assert field in str(update.whereclause)
    assert "source_review_resolution" not in candidate.metadata_json
