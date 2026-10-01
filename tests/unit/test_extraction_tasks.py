from __future__ import annotations

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

from api.db.models import Conversation
from api.db.models import ConversationProcessingStatus
from api.db.models import MemorySourceEvent
from api.db.models import ProxyUser
from api.db.models import User
from api.db.models import PendingExtractionCandidate
from api.schemas.extraction_schemas import PendingExtractedMemory
from api.services.extractor import ExtractedMemory
from api.tasks import extraction_tasks


class FakeSession:
    def __init__(self, proxy_user: ProxyUser) -> None:
        self.proxy_user = proxy_user
        self.added: list[object] = []
        self.commits = 0
        self.rollbacks = 0
        self.closed = False
        self.source_event = None

    def get(self, model, identifier):
        if model is ProxyUser and identifier == self.proxy_user.id:
            return self.proxy_user
        if model is MemorySourceEvent and self.source_event is not None:
            return self.source_event
        return None

    def add(self, item) -> None:
        self.added.append(item)

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1

    def close(self) -> None:
        self.closed = True


class FakeSessionFactory:
    def __init__(self, session: FakeSession) -> None:
        self.session = session

    def __call__(self):
        return self.session


class FakeExtractor:
    def extract(self, messages, user_id):
        return [
            ExtractedMemory(
                content="User prefers Python",
                category="preference",
                importance_score=7.0,
                confidence=0.92,
                expiry="permanent",
                reasoning="Explicit preference",
            )
        ]


class FakeScorer:
    def score(self, memory, user_context):
        return float(memory.importance_score) + 1.0


class FakeConflictResolver:
    def __init__(self) -> None:
        self.calls = []

    def check_and_store(self, memories, **kwargs):
        self.calls.append({"memories": memories, **kwargs})
        return [
            SimpleNamespace(
                id=str(uuid.uuid4()),
                user_id=kwargs["user_id"],
                proxy_user_id=kwargs["proxy_user_id"],
                content=memories[0].content,
                category=memories[0].category,
                importance_score=memories[0].importance_score,
                confidence_score=memories[0].confidence,
                previous_version_id=None,
                resolution="NEW",
            )
        ]


class CapturingConflictResolver(FakeConflictResolver):
    instance: CapturingConflictResolver | None = None

    def __init__(self, **kwargs) -> None:
        super().__init__()
        self.provenance_snapshot = kwargs["provenance_snapshot"]
        CapturingConflictResolver.instance = self


class StructuredClarificationResolver(FakeConflictResolver):
    def __init__(self) -> None:
        super().__init__()
        self.clarification_calls: list[dict[str, object]] = []

    def check_and_store(self, memories, **kwargs):
        self.calls.append({"memories": memories, **kwargs})
        return []

    def queue_existing_memory_clarification(self, **kwargs) -> bool:
        self.clarification_calls.append(kwargs)
        return True


def test_run_extraction_pipeline_persists_via_conflict_resolver(monkeypatch) -> None:
    proxy_user = ProxyUser(
        id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        external_user_id="ext-123",
        external_user_id_hash="hash-123",
        memory_count=2,
        metadata_json={},
        is_blocked=False,
    )
    session = FakeSession(proxy_user)
    session_factory = FakeSessionFactory(session)
    resolver = FakeConflictResolver()
    resolver.last_pending_candidates = [PendingExtractedMemory(
        content="Python might become my default.", category="preference",
        importance_score=7.0, confidence=0.9, reasoning="No committed choice.",
        candidate_reason="source_decision_pending",
        validated_evidence={"source_decision": {"action": "USER_REVIEW"}},
    )]
    resolver.last_source_decision_tokens_used = 77
    resolver.last_source_decision_calls = 2
    resolver.last_source_decision_wall_latency_ms = 121
    resolver.last_user_clarifications_queued = 1
    buffered = []
    def capture_pending(_session, **kwargs):
        buffered.extend(kwargs["candidates"])
        return len(kwargs["candidates"])
    monkeypatch.setattr(extraction_tasks, "_persist_pending_extraction_candidates", capture_pending)
    backing_user = User(
        id=uuid.uuid4(),
        external_id=f"proxy::{proxy_user.id}",
        email="proxy@example.test",
        settings={},
        memory_count=0,
        is_active=True,
    )
    conversation = Conversation(
        id=uuid.uuid4(),
        user_id=backing_user.id,
        message_count=2,
        processing_status=ConversationProcessingStatus.processing,
    )

    invalidated = {}
    refreshed = {}

    monkeypatch.setattr(
        extraction_tasks,
        "_ensure_proxy_backing_user",
        lambda _session, _proxy_user_id: backing_user,
    )
    monkeypatch.setattr(
        extraction_tasks,
        "_create_source_conversation",
        lambda _session, **kwargs: conversation,
    )
    monkeypatch.setattr(
        extraction_tasks,
        "_refresh_proxy_user_memory_count",
        lambda _session, proxy_user_id: refreshed.setdefault("proxy_user_id", str(proxy_user_id)),
    )
    monkeypatch.setattr(
        extraction_tasks,
        "_invalidate_proxy_user_cache",
        lambda proxy_user_id: invalidated.setdefault("proxy_user_id", proxy_user_id),
    )

    result = extraction_tasks.run_extraction_pipeline(
        {
            "job_id": "job-123",
            "tenant_id": str(proxy_user.tenant_id),
            "proxy_user_id": str(proxy_user.id),
            "external_user_id": "ext-123",
            "agent_id": None,
            "messages": [
                {"role": "user", "content": "I prefer Python"},
                {"role": "assistant", "content": "Noted"},
            ],
            "metadata": {"session_id": "sess-1"},
        },
        session_factory=session_factory,
        extractor=FakeExtractor(),
        scorer=FakeScorer(),
        qdrant_service=SimpleNamespace(),
        conflict_resolver=resolver,
        client=SimpleNamespace(),
    )

    assert result["status"] == "processed"
    assert result["memories_created"] == 1
    assert result["pending_candidates_buffered"] == 1
    assert result["clarification_queued"] is True
    assert result["tokens_used"] == 77
    assert result["extraction_metadata"]["source_decision"] == {
        "pending_count": 1, "tokens_used": 77, "complete_calls": 2, "wall_latency_ms": 121,
    }
    assert buffered == resolver.last_pending_candidates
    assert result["stored_memories"][0]["proxy_user_id"] == str(proxy_user.id)
    assert resolver.calls[0]["tenant_id"] == str(proxy_user.tenant_id)
    assert resolver.calls[0]["proxy_user_id"] == str(proxy_user.id)
    assert resolver.calls[0]["source_conversation_id"] == str(conversation.id)
    assert resolver.calls[0]["auto_commit"] is False
    assert resolver.calls[0]["clarification_requested"] is False
    assert resolver.calls[0]["memories"][0].importance_score == 8.0
    assert invalidated["proxy_user_id"] == str(proxy_user.id)
    assert refreshed["proxy_user_id"] == str(proxy_user.id)
    assert conversation.processing_status == ConversationProcessingStatus.done
    assert session.commits == 2
    assert session.rollbacks == 0
    assert session.closed is True


def test_source_decision_pending_persists_evidence_without_creating_memory(monkeypatch) -> None:
    proxy = ProxyUser(id=uuid.uuid4(), tenant_id=uuid.uuid4())
    session = FakeSession(proxy)
    candidate = PendingExtractedMemory(
        content="Python might become my default.", category="preference",
        importance_score=7.0, confidence=0.9, reasoning="No committed choice.",
        candidate_reason="source_decision_pending",
        validated_evidence={"source_spans": [{"turn_sha256": "digest"}],
                            "source_decision": {"action": "USER_REVIEW"}},
    )
    monkeypatch.setattr(extraction_tasks, "_find_matching_pending_candidate", lambda *args, **kwargs: (None, False))
    count = extraction_tasks._persist_pending_extraction_candidates(
        session, candidates=[candidate], tenant_id=str(proxy.tenant_id),
        proxy_user_id=str(proxy.id), extraction_job_id=str(uuid.uuid4()), source_event_id=None,
    )
    assert count == 1
    assert len(session.added) == 1
    row = session.added[0]
    assert isinstance(row, PendingExtractionCandidate)
    assert row.status == "pending"
    assert row.candidate_reason == "source_decision_pending"
    assert row.metadata_json["extraction_evidence"] == candidate.validated_evidence
    assert row.proxy_user_id == proxy.id
    assert row.tenant_id == proxy.tenant_id


def test_external_conversation_id_survives_processing_in_memory_provenance(monkeypatch) -> None:
    proxy_user = ProxyUser(
        id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        external_user_id="ext-conversation-provenance",
        external_user_id_hash="hash-conversation-provenance",
        memory_count=0,
        metadata_json={},
        is_blocked=False,
    )
    session = FakeSession(proxy_user)
    backing_user = User(
        id=uuid.uuid4(),
        external_id=f"proxy::{proxy_user.id}",
        email="provenance@example.test",
        settings={},
        memory_count=0,
        is_active=True,
    )
    conversation = Conversation(
        id=uuid.uuid4(),
        user_id=backing_user.id,
        message_count=1,
        processing_status=ConversationProcessingStatus.processing,
    )
    CapturingConflictResolver.instance = None
    monkeypatch.setattr(extraction_tasks, "_ensure_proxy_backing_user", lambda *_args: backing_user)
    monkeypatch.setattr(extraction_tasks, "_create_source_conversation", lambda *_args, **_kwargs: conversation)
    monkeypatch.setattr(extraction_tasks, "_refresh_proxy_user_memory_count", lambda *_args: None)
    monkeypatch.setattr(extraction_tasks, "_invalidate_proxy_user_cache", lambda *_args: None)
    monkeypatch.setattr(extraction_tasks, "ConflictResolver", CapturingConflictResolver)

    extraction_tasks.run_extraction_pipeline(
        {
            "job_id": "job-external-conversation",
            "tenant_id": str(proxy_user.tenant_id),
            "proxy_user_id": str(proxy_user.id),
            "external_conversation_id": "vscode-chat-2026-09-12-01",
            "messages": [{"role": "user", "content": "I prefer Python"}],
            "evidence_policy": {"authority_priority": 20, "attestation": "client_asserted"},
        },
        session_factory=FakeSessionFactory(session),
        extractor=FakeExtractor(),
        scorer=FakeScorer(),
        qdrant_service=SimpleNamespace(),
        client=SimpleNamespace(),
    )

    assert CapturingConflictResolver.instance is not None
    provenance = CapturingConflictResolver.instance.provenance_snapshot
    assert provenance["external_conversation_id"] == "vscode-chat-2026-09-12-01"
    assert provenance["attestation"] == "client_asserted"


def test_source_event_authority_is_not_overwritten_by_submission_policy(monkeypatch) -> None:
    proxy_user = ProxyUser(
        id=uuid.uuid4(), tenant_id=uuid.uuid4(), external_user_id="source-policy-user",
        external_user_id_hash="source-policy-hash", memory_count=0, metadata_json={}, is_blocked=False,
    )
    session = FakeSession(proxy_user)
    source_event_id = uuid.uuid4()
    session.source_event = SimpleNamespace(
        id=source_event_id,
        source_event_id="registered-event-1",
        source_service="trusted-service",
        writer_id=None,
        writer=SimpleNamespace(authority_rules={"default_priority": 90}),
        observed_at=datetime.now(UTC),
        received_at=None,
        payload_hash="hash",
        scope={},
        evidence_refs=[],
        processing_metadata={},
    )
    backing_user = User(
        id=uuid.uuid4(), external_id=f"proxy::{proxy_user.id}", email="source@example.test",
        settings={}, memory_count=0, is_active=True,
    )
    conversation = Conversation(
        id=uuid.uuid4(), user_id=backing_user.id, message_count=1,
        processing_status=ConversationProcessingStatus.processing,
    )
    CapturingConflictResolver.instance = None
    monkeypatch.setattr(extraction_tasks, "_ensure_proxy_backing_user", lambda *_args: backing_user)
    monkeypatch.setattr(extraction_tasks, "_create_source_conversation", lambda *_args, **_kwargs: conversation)
    monkeypatch.setattr(extraction_tasks, "_refresh_proxy_user_memory_count", lambda *_args: None)
    monkeypatch.setattr(extraction_tasks, "_invalidate_proxy_user_cache", lambda *_args: None)
    monkeypatch.setattr(extraction_tasks, "ConflictResolver", CapturingConflictResolver)

    extraction_tasks.run_extraction_pipeline(
        {
            "job_id": "job-source-policy", "tenant_id": str(proxy_user.tenant_id),
            "proxy_user_id": str(proxy_user.id), "source_event_id": str(source_event_id),
            "messages": [{"role": "user", "content": "I prefer Python"}],
            "evidence_policy": {"authority_priority": 20, "attestation": "client_asserted"},
            "external_conversation_id": "release-check-001",
        },
        session_factory=FakeSessionFactory(session), extractor=FakeExtractor(), scorer=FakeScorer(),
        qdrant_service=SimpleNamespace(), client=SimpleNamespace(),
    )

    provenance = CapturingConflictResolver.instance.provenance_snapshot
    assert provenance["authority_rules"] == {"default_priority": 90}
    assert provenance["submission_evidence_policy"]["attestation"] == "client_asserted"
    assert provenance["external_conversation_id"] == "release-check-001"


def test_phrase_alone_does_not_bypass_structured_uncertainty_routing(monkeypatch) -> None:
    proxy_user = ProxyUser(
        id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        external_user_id="ext-clarify",
        external_user_id_hash="hash-clarify",
        memory_count=0,
        metadata_json={},
        is_blocked=False,
    )
    session = FakeSession(proxy_user)
    resolver = FakeConflictResolver()
    backing_user = User(
        id=uuid.uuid4(),
        external_id=f"proxy::{proxy_user.id}",
        email="proxy@example.test",
        settings={},
        memory_count=0,
        is_active=True,
    )
    conversation = Conversation(
        id=uuid.uuid4(),
        user_id=backing_user.id,
        message_count=1,
        processing_status=ConversationProcessingStatus.processing,
    )
    monkeypatch.setattr(extraction_tasks, "_ensure_proxy_backing_user", lambda *_args: backing_user)
    monkeypatch.setattr(extraction_tasks, "_create_source_conversation", lambda *_args, **_kwargs: conversation)
    monkeypatch.setattr(extraction_tasks, "_refresh_proxy_user_memory_count", lambda *_args: None)
    monkeypatch.setattr(extraction_tasks, "_invalidate_proxy_user_cache", lambda *_args: None)

    extraction_tasks.run_extraction_pipeline(
        {
            "job_id": "job-clarify",
            "tenant_id": str(proxy_user.tenant_id),
            "proxy_user_id": str(proxy_user.id),
            "messages": [
                {"role": "user", "content": "I am unsure whether this replaces my earlier preference."}
            ],
        },
        session_factory=FakeSessionFactory(session),
        extractor=FakeExtractor(),
        scorer=FakeScorer(),
        qdrant_service=SimpleNamespace(),
        conflict_resolver=resolver,
        client=SimpleNamespace(),
    )

    assert resolver.calls[0]["clarification_requested"] is False


def test_zero_extraction_structured_clarification_is_queued_without_new_memory(
    monkeypatch,
) -> None:
    proxy_user = ProxyUser(
        id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        external_user_id="ext-structured-clarify",
        external_user_id_hash="hash-structured-clarify",
        memory_count=2,
        metadata_json={},
        is_blocked=False,
    )
    session = FakeSession(proxy_user)
    resolver = StructuredClarificationResolver()
    backing_user = User(
        id=uuid.uuid4(),
        external_id=f"proxy::{proxy_user.id}",
        email="proxy@example.test",
        settings={},
        memory_count=2,
        is_active=True,
    )
    conversation = Conversation(
        id=uuid.uuid4(),
        user_id=backing_user.id,
        message_count=1,
        processing_status=ConversationProcessingStatus.processing,
    )
    selected_ids = [str(uuid.uuid4()), str(uuid.uuid4())]
    monkeypatch.setattr(
        extraction_tasks,
        "_extract_memories_for_pipeline",
        lambda *_args, **_kwargs: (
            [],
            {
                "nothing_to_extract": True,
                "clarification_request": {"memory_ids": selected_ids},
                "extraction_metadata": {
                    "memory_clarification": {"requested": True}
                },
            },
            False,
        ),
    )
    monkeypatch.setattr(
        extraction_tasks,
        "_ensure_proxy_backing_user",
        lambda *_args: backing_user,
    )
    monkeypatch.setattr(
        extraction_tasks,
        "_create_source_conversation",
        lambda *_args, **_kwargs: conversation,
    )
    monkeypatch.setattr(
        extraction_tasks,
        "_refresh_proxy_user_memory_count",
        lambda *_args: None,
    )
    monkeypatch.setattr(
        extraction_tasks,
        "_invalidate_proxy_user_cache",
        lambda *_args: None,
    )

    result = extraction_tasks.run_extraction_pipeline(
        {
            "job_id": "job-structured-clarify",
            "tenant_id": str(proxy_user.tenant_id),
            "proxy_user_id": str(proxy_user.id),
            "messages": [
                {
                    "role": "user",
                    "content": "Do not decide; ask me to choose.",
                }
            ],
        },
        session_factory=FakeSessionFactory(session),
        extractor=FakeExtractor(),
        scorer=FakeScorer(),
        qdrant_service=SimpleNamespace(),
        conflict_resolver=resolver,
        client=SimpleNamespace(),
    )

    assert resolver.calls[0]["memories"] == []
    assert resolver.calls[0]["clarification_requested"] is False
    assert resolver.clarification_calls == [
        {
            "memory_ids": selected_ids,
            "tenant_id": str(proxy_user.tenant_id),
            "proxy_user_id": str(proxy_user.id),
        }
    ]
    assert result["memories_created"] == 0
    assert result["clarification_queued"] is True

def test_pending_candidate_similarity_allows_rephrased_reinforcement() -> None:
    existing = SimpleNamespace(content="User may prefer short replies for difficult topics")
    candidate = extraction_tasks.PendingExtractedMemory(
        content="User prefers short replies for difficult topics",
        category="preference",
        importance_score=6.0,
        confidence=0.58,
        reasoning="Weak repeated preference",
    )

    assert extraction_tasks._candidate_similarity(existing.content, candidate.content) >= 0.82
    assert extraction_tasks._can_reinforce_candidate(existing, candidate) is True


def test_pending_candidate_polarity_guard_blocks_opposite_meaning() -> None:
    existing = SimpleNamespace(content="User prefers short replies for difficult topics")
    candidate = extraction_tasks.PendingExtractedMemory(
        content="User does not prefer short replies for difficult topics",
        category="preference",
        importance_score=6.0,
        confidence=0.58,
        reasoning="Opposite weak preference",
    )

    assert extraction_tasks._candidate_similarity(existing.content, candidate.content) >= 0.82
    assert extraction_tasks._can_reinforce_candidate(existing, candidate) is False


def test_uncertain_change_routes_to_governance_without_generic_buffering() -> None:
    uncertain = extraction_tasks.PendingExtractedMemory(
        content="User may replace C++ with Python but has not decided.",
        category="preference",
        importance_score=8.0,
        confidence=0.58,
        reasoning="The replacement is explicitly undecided.",
        candidate_reason="uncertain_change",
        validated_evidence={"relation": "direct_user_statement"},
    )
    ordinary = extraction_tasks.PendingExtractedMemory(
        content="User may prefer short explanations.",
        category="preference",
        importance_score=5.0,
        confidence=0.58,
        reasoning="Borderline preference.",
    )

    regular, routed = extraction_tasks._route_uncertain_changes(
        [uncertain, ordinary]
    )

    assert regular == [ordinary]
    assert len(routed) == 1
    assert routed[0].validated_evidence["governance_directive"] == (
        "clarify_if_conflict"
    )





class _ScalarResult:
    def __init__(self, value) -> None:
        self.value = value

    def scalar_one_or_none(self):
        return self.value


class _RowcountResult:
    def __init__(self, rowcount: int) -> None:
        self.rowcount = rowcount


class _ProposalClaimSession:
    def __init__(self, proposal) -> None:
        self.proposal = proposal
        self.calls = 0

    def execute(self, _statement):
        self.calls += 1
        if self.calls == 1:
            return _ScalarResult(self.proposal)
        return _RowcountResult(1)


def test_confirmed_proposal_is_claimed_before_memory_storage() -> None:
    tenant_id = uuid.uuid4()
    proxy_user_id = uuid.uuid4()
    proposal_id = uuid.uuid4()
    proposal = SimpleNamespace(
        id=proposal_id,
        proposal_group_id="group-1",
    )
    memory = SimpleNamespace(
        validated_evidence={
            "relation": "user_confirmed_assistant_proposal",
            "proposal": {"id": str(proposal_id)},
        }
    )
    session = _ProposalClaimSession(proposal)

    accepted, rejected = extraction_tasks._claim_confirmed_proposals(
        session,
        memories=[memory],
        tenant_id=str(tenant_id),
        proxy_user_id=str(proxy_user_id),
        conversation_scope_id="external:chat-1",
    )

    assert accepted == [memory]
    assert rejected == 0
    assert session.calls == 3


def test_confirmation_is_dropped_if_proposal_is_no_longer_active() -> None:
    proposal_id = uuid.uuid4()
    memory = SimpleNamespace(
        validated_evidence={
            "relation": "user_confirmed_assistant_proposal",
            "proposal": {"id": str(proposal_id)},
        }
    )
    session = _ProposalClaimSession(None)

    accepted, rejected = extraction_tasks._claim_confirmed_proposals(
        session,
        memories=[memory],
        tenant_id=str(uuid.uuid4()),
        proxy_user_id=str(uuid.uuid4()),
        conversation_scope_id="external:chat-1",
    )

    assert accepted == []
    assert rejected == 1
    assert session.calls == 1

def test_parse_optional_uuid_ignores_non_uuid_agent_labels() -> None:
    from api.tasks.extraction_tasks import _parse_optional_uuid

    assert _parse_optional_uuid(None) is None
    assert _parse_optional_uuid("support-bot") is None
    valid = uuid.uuid4()
    assert _parse_optional_uuid(str(valid)) == valid
