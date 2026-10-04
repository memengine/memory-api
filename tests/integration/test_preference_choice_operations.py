"""Real disposable PostgreSQL; intent is controlled, all real providers forbidden."""
import asyncio
import os
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from api.db.models import (
    AuditLog,
    ExtractionJob,
    Memory,
    MemoryClaim,
    MemoryClaimRevision,
    PendingExtractionCandidate,
    ProxyUser,
    User,
    VectorSyncOutbox,
)
from api.errors import APIError
from api.schemas.preferences import PreferenceReadContext
from api.services.conflict_resolver import ConflictResolver
from api.services.preference_choice_service import CHOICE_OPERATION_MARKER
from api.services.preference_snapshot_service import load_preference_snapshot
from api.services.source_review_service import list_source_reviews
from api.settings import get_settings
from api.tasks import extraction_tasks
from api.tasks.source_review_tasks import run_source_review_maintenance
from tests.integration.test_preference_choice_policy import answer, read_decisions
from tests.integration.test_preference_choice_policy import snapshot as stored
from tests.integration.test_preference_snapshots import identity, offered, seed
from tests.integration.test_source_decision_postgres import _read_preferences
from tests.integration.test_source_decision_postgres import sql_scope as _sql_scope
from tests.unit.test_preference_choice_service import request

sql_scope = _sql_scope
PROJECT = {"kind": "context", "namespace": "project", "key": "Cedar"}


@pytest.fixture(autouse=True)
def forbid_real_providers(monkeypatch):
    def forbidden(*_args, **_kwargs):
        pytest.fail("choice operations must not call a provider")
    for method in ("complete", "complete_sync"):
        monkeypatch.setattr(f"api.services.llm_service.LLMService.{method}", forbidden)
    for method in ("embed", "embed_sync"):
        monkeypatch.setattr(f"api.services.embedding_service.EmbeddingService.{method}", forbidden)


def read(scope):
    return load_preference_snapshot(scope.factory, tenant_id=scope.tenant_id,
        proxy_user_id=scope.proxy_id, catalog=offered(scope))


def apply(scope, *, snap=None, requests=None, messages=None, source_id=None, persist=None, user_id=None):
    snap = snap or read(scope)
    requests = requests or [request()]
    messages = messages if messages is not None else [{"role": "user", "content": requests[0]["quote"]}]
    source_id = source_id or uuid.uuid4()
    job_id = uuid.uuid4()
    def forbidden(*_args, **_kwargs):
        pytest.fail("choice operations must not embed or classify")
    with scope.factory.begin() as session:
        backing = session.scalars(select(User).where(User.external_id == f"proxy::{scope.proxy_id}")).one()
        session.add(ExtractionJob(id=job_id, tenant_id=scope.tenant_id, proxy_user_id=scope.proxy_id,
            external_user_id=scope.external_id, payload={"messages": messages}))
        session.flush()
        resolver = ConflictResolver(session=session, qdrant_service=SimpleNamespace(), embedder=forbidden,
            llm_service=SimpleNamespace(), source_messages=messages)
        rows = resolver.apply_preference_choices(requests, snapshot=snap,
            user_id=str(user_id or backing.id), source_conversation_id=source_id)
        assert rows == []
        diagnostics = {}
        buffered = (persist or extraction_tasks._persist_pending_extraction_candidates)(session,
            candidates=resolver.last_pending_candidates, tenant_id=str(scope.tenant_id),
            proxy_user_id=str(scope.proxy_id), extraction_job_id=str(job_id), source_event_id=None,
            diagnostics=diagnostics)
        result = SimpleNamespace(withheld=resolver.last_preference_choices_withheld,
            reaffirmed=resolver.last_preference_choices_reaffirmed,
            rejected=resolver.last_preference_choices_rejected, buffered=buffered, diagnostics=diagnostics)
    return result, job_id


def review_id(scope, job_id):
    with scope.factory() as session:
        return session.scalars(select(PendingExtractionCandidate).where(
            PendingExtractionCandidate.extraction_job_id == job_id)).one().id


def counts(scope):
    with scope.factory() as session:
        return tuple(session.scalar(select(func.count()).select_from(model))
            for model in (Memory, MemoryClaimRevision, VectorSyncOutbox, AuditLog))


def test_value_free_hold_blocks_only_exact_scope_and_keeps_authority(sql_scope, monkeypatch):
    general = seed(sql_scope, monkeypatch)
    target = seed(sql_scope, monkeypatch, value="Python", scope=PROJECT)
    before = counts(sql_scope)
    result, job = apply(sql_scope, requests=[request(scope_ref="cedar")])
    assert result.withheld == result.buffered == 1 and result.reaffirmed == 0 and result.rejected == {}
    held, status, authority = stored(sql_scope, str(target))
    assert status == "disputed" and authority == 20 and not held.is_archived
    assert counts(sql_scope) == (*before[:3], before[3] + 1)
    rows, decisions = read_decisions(sql_scope, PROJECT)
    assert rows == [] and len(decisions) == 1
    assert [row.id for row in _read_preferences(sql_scope, monkeypatch, {"kind": "global"})] == [str(general)]
    assert [row.id for row in _read_preferences(sql_scope, monkeypatch, {**PROJECT, "key": "Other"})] == [str(general)]
    with sql_scope.factory() as session:
        candidate = session.get(PendingExtractionCandidate, review_id(sql_scope, job))
        descriptor = candidate.metadata_json["extraction_evidence"][CHOICE_OPERATION_MARKER]
        assert candidate.content == request()["quote"] and "value" not in descriptor
        audit = session.scalars(select(AuditLog).where(AuditLog.memory_id == target)
            .order_by(AuditLog.created_at.desc())).first()
        assert audit.new_value["source_spans"] and audit.new_value["choice_operation"] == "withhold"


@pytest.mark.parametrize("method", ["operation", "review"])
def test_value_free_reaffirmation_preserves_value_and_authority(sql_scope, monkeypatch, method):
    target = seed(sql_scope, monkeypatch)
    _, job = apply(sql_scope)
    held = identity(read(sql_scope)).target
    before = counts(sql_scope)
    if method == "review":
        assert answer(sql_scope, review_id(sql_scope, job), "keep_current").data.resolved
    else:
        result, _ = apply(sql_scope, requests=[request(action="reaffirm", quote="Keep my stored default.")])
        assert result.reaffirmed == 1 and result.withheld == result.buffered == 0
    now = identity(read(sql_scope)).target
    assert now.memory_id == target and now.value == held.value == "C++" and now.authority == held.authority == 20
    assert now.choice_state == "current" and now.memory_version != held.memory_version
    assert counts(sql_scope) == (*before[:3], before[3] + 1)
    with pytest.raises(APIError):
        answer(sql_scope, review_id(sql_scope, job), "keep_current")


@pytest.mark.parametrize("close", ["dismiss", "expire", "purge"])
def test_closing_review_does_not_restore_default(sql_scope, monkeypatch, close):
    seed(sql_scope, monkeypatch)
    _, job = apply(sql_scope)
    candidate_id = review_id(sql_scope, job)
    if close == "dismiss":
        assert answer(sql_scope, candidate_id, "dismiss").data.resolved
    else:
        with sql_scope.factory.begin() as session:
            session.get(PendingExtractionCandidate, candidate_id).created_at = datetime.now(UTC) - timedelta(days=40 if close == "purge" else 8)
        run_source_review_maintenance(session_factory=sql_scope.factory, tenant_id=sql_scope.tenant_id)
    assert read_decisions(sql_scope, {"kind": "global"})[0] == []
    assert identity(read(sql_scope)).target.choice_state == "current_choice_unresolved"


@pytest.mark.parametrize("action", ["withhold", "reaffirm"])
def test_exact_retry_does_not_churn_state_review_or_audit(sql_scope, monkeypatch, action):
    seed(sql_scope, monkeypatch)
    if action == "reaffirm":
        apply(sql_scope)
    snap, source = read(sql_scope), uuid.uuid4()
    requests = [request(action=action)]
    result, _ = apply(sql_scope, snap=snap, source_id=source, requests=requests)
    assert result.withheld + result.reaffirmed == 1
    before, count_before = read(sql_scope), counts(sql_scope)
    retry, _ = apply(sql_scope, snap=snap, source_id=source, requests=requests)
    assert retry.withheld == retry.reaffirmed == retry.buffered == 0 and retry.rejected == {}
    assert read(sql_scope) == before and counts(sql_scope) == count_before


def test_old_operation_and_review_cannot_clear_newer_hold(sql_scope, monkeypatch):
    seed(sql_scope, monkeypatch)
    _, old_job = apply(sql_scope)
    reaffirm_snapshot = read(sql_scope)
    apply(sql_scope, snap=reaffirm_snapshot, requests=[request(action="reaffirm")])
    apply(sql_scope, requests=[request(quote="I am still undecided about the default.")])
    before = read(sql_scope)
    old, _ = apply(sql_scope, snap=reaffirm_snapshot, requests=[request(action="reaffirm")])
    assert old.rejected == {"preference_target_stale": 1} and old.reaffirmed == 0
    with pytest.raises(APIError):
        answer(sql_scope, review_id(sql_scope, old_job), "keep_current")
    assert read(sql_scope) == before


@pytest.mark.parametrize("kind", ["version", "ledger_version", "ledger_authority", "provenance_authority", "archived", "expired", "future", "revision", "ledger", "absent_ledger", "absent_winner"])
def test_changed_or_ineligible_target_fails_without_mutation(sql_scope, monkeypatch, kind):
    target = seed(sql_scope, monkeypatch)
    snap = read(sql_scope)
    with sql_scope.factory.begin() as session:
        memory = session.get(Memory, target)
        ledger = session.scalars(select(MemoryClaim).where(MemoryClaim.active_memory_id == target)).one()
        if kind == "version":
            memory.metadata_json = {**memory.metadata_json, "unrelated_edit": True}
        elif kind == "ledger_version":
            ledger.updated_at = datetime.now(UTC) + timedelta(seconds=1)
        elif kind == "ledger_authority":
            ledger.authority_priority = 60
        elif kind == "provenance_authority":
            memory.metadata_json = {**memory.metadata_json,
                "provenance": {**memory.metadata_json["provenance"], "authority_rules": {"default_priority": 60}}}
        elif kind == "archived":
            memory.is_archived = True
            ledger.status = "archived"
            session.get(MemoryClaimRevision, ledger.winning_revision_id).status = "archived"
        elif kind == "expired":
            memory.expires_at = datetime.now(UTC) - timedelta(seconds=1)
        elif kind == "future":
            memory.effective_from = datetime.now(UTC) + timedelta(days=1)
        elif kind == "revision":
            session.get(MemoryClaimRevision, ledger.winning_revision_id).asserted_value = "Invalid"
        elif kind == "absent_ledger":
            session.delete(ledger)
        elif kind == "absent_winner":
            ledger.active_memory_id = None
        else:
            ledger.active_value = "Invalid"
    with sql_scope.factory() as session:
        memory = session.get(Memory, target)
        before = (memory.content, memory.metadata_json, memory.is_archived)
    count_before = counts(sql_scope)
    result, _ = apply(sql_scope, snap=snap)
    assert result.withheld == result.reaffirmed == result.buffered == 0 and sum(result.rejected.values()) == 1
    with sql_scope.factory() as session:
        memory = session.get(Memory, target)
        assert (memory.content, memory.metadata_json, memory.is_archived) == before
    assert counts(sql_scope) == count_before


@pytest.mark.parametrize("kind", ["blocked", "inactive", "foreign_backing"])
def test_unavailable_principal_cannot_apply(sql_scope, monkeypatch, kind):
    target = seed(sql_scope, monkeypatch)
    snap = read(sql_scope)
    with sql_scope.factory.begin() as session:
        if kind == "blocked":
            session.get(ProxyUser, sql_scope.proxy_id).is_blocked = True
        elif kind == "inactive":
            session.scalars(select(User).where(User.external_id == f"proxy::{sql_scope.proxy_id}")).one().is_active = False
    before = counts(sql_scope)
    with pytest.raises(ValueError, match="owner unavailable"):
        apply(sql_scope, snap=snap, user_id=uuid.uuid4() if kind == "foreign_backing" else None)
    assert counts(sql_scope) == before and stored(sql_scope, str(target))[1] == "active"


@pytest.mark.parametrize("role", ["assistant", "tool", "system", "mismatch", "untrusted_source"])
def test_forged_evidence_never_changes_state(sql_scope, monkeypatch, role):
    target = seed(sql_scope, monkeypatch)
    message = {"role": role, "content": request()["quote"]}
    if role == "mismatch":
        message = {"role": "user", "content": "Different evidence."}
    elif role == "untrusted_source":
        message = {"role": "user", "source_kind": "tool_result", "content": request()["quote"]}
    before = counts(sql_scope)
    with pytest.raises(ValueError):
        apply(sql_scope, messages=[message])
    assert counts(sql_scope) == before and stored(sql_scope, str(target))[1] == "active"


def test_pending_failure_rolls_back_hold_and_audit(sql_scope, monkeypatch):
    seed(sql_scope, monkeypatch)
    before, count_before = read(sql_scope), counts(sql_scope)
    def fail(*_args, **_kwargs):
        raise RuntimeError("controlled review persistence failure")
    with pytest.raises(RuntimeError, match="controlled"):
        apply(sql_scope, persist=fail)
    assert read(sql_scope) == before and counts(sql_scope) == count_before


def test_review_capacity_rejection_preserves_hold(sql_scope, monkeypatch):
    seed(sql_scope, monkeypatch)
    seed(sql_scope, monkeypatch, value="Python", scope=PROJECT)
    monkeypatch.setattr(get_settings(), "source_review_profile_row_limit", 1)
    apply(sql_scope, requests=[request(scope_ref="cedar")])
    result, _ = apply(sql_scope)
    assert result.withheld == 1 and result.buffered == 0 and result.diagnostics["source_reviews_capacity_rejected"] == 1
    assert read_decisions(sql_scope, {"kind": "global"})[0] == []


def test_concurrent_same_snapshot_only_one_distinct_operation_applies(sql_scope, monkeypatch):
    seed(sql_scope, monkeypatch)
    snap = read(sql_scope)
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(apply, sql_scope, snap=snap, requests=[request(quote=quote)])
            for quote in ("I haven't decided which default to keep.", "I am undecided about the default.")]
        results = [future.result(timeout=15)[0] for future in futures]
    assert sorted(result.withheld for result in results) == [0, 1]
    assert sum(result.rejected.get("preference_target_stale", 0) for result in results) == 1


def test_reviews_are_returned_only_for_exact_preference_context(sql_scope, monkeypatch):
    seed(sql_scope, monkeypatch, scope=PROJECT)
    apply(sql_scope, requests=[request(scope_ref="cedar")])
    async def exercise():
        engine = create_async_engine(os.environ["DATABASE_URL"])
        try:
            async with async_sessionmaker(engine)() as session:
                async def listing(context):
                    return await list_source_reviews(session, tenant_id=str(sql_scope.tenant_id),
                        proxy_user_id=str(sql_scope.proxy_id), preference_context=context)
                exact = PreferenceReadContext(applicability=PROJECT, property_keys=["examples.language"])
                general = PreferenceReadContext(applicability={"kind": "global"}, property_keys=["examples.language"])
                assert len(await listing(exact)) == 1
                assert await listing(general) == await listing(None) == []
        finally:
            await engine.dispose()
    asyncio.run(exercise())


def test_reaffirm_requires_an_existing_hold(sql_scope, monkeypatch):
    seed(sql_scope, monkeypatch)
    before = read(sql_scope)
    result, _ = apply(sql_scope, requests=[request(action="reaffirm")])
    assert result.rejected == {"preference_choice_not_held": 1} and read(sql_scope) == before


def test_disabled_policy_cannot_be_bypassed_by_internal_operation(sql_scope, monkeypatch):
    seed(sql_scope, monkeypatch)
    before = read(sql_scope)
    monkeypatch.setattr(get_settings(), "explicit_preference_claims_enabled", False)
    with pytest.raises(ValueError, match="preferences unavailable"):
        apply(sql_scope)
    assert read(sql_scope) == before


@pytest.mark.parametrize("kind", ["inactive", "descriptor", "mixed_markers", "authority"])
def test_review_cannot_bypass_changed_principal_or_bad_descriptor(sql_scope, monkeypatch, kind):
    target = seed(sql_scope, monkeypatch)
    _, job = apply(sql_scope)
    candidate_id = review_id(sql_scope, job)
    with sql_scope.factory.begin() as session:
        if kind == "inactive":
            session.scalars(select(User).where(User.external_id == f"proxy::{sql_scope.proxy_id}")).one().is_active = False
        elif kind == "authority":
            session.scalars(select(MemoryClaim).where(MemoryClaim.active_memory_id == target)).one().authority_priority = 60
        else:
            candidate = session.get(PendingExtractionCandidate, candidate_id)
            evidence = dict(candidate.metadata_json["extraction_evidence"])
            if kind == "descriptor":
                evidence[CHOICE_OPERATION_MARKER] = {**evidence[CHOICE_OPERATION_MARKER], "claim_id": str(uuid.uuid4())}
            else:
                evidence["explicit_preference_v1"] = {}
            candidate.metadata_json = {**candidate.metadata_json, "extraction_evidence": evidence}
    with pytest.raises(APIError):
        answer(sql_scope, candidate_id, "keep_current")
    assert stored(sql_scope, str(target))[1] == "disputed"


def test_ledger_lock_contention_is_retryable_for_operation_review(sql_scope, monkeypatch):
    target = seed(sql_scope, monkeypatch)
    _, job = apply(sql_scope)
    candidate_id = review_id(sql_scope, job)
    with sql_scope.factory.begin() as session:
        session.execute(select(MemoryClaim).where(MemoryClaim.active_memory_id == target).with_for_update()).one()
        with pytest.raises(APIError) as error:
            answer(sql_scope, candidate_id, "keep_current")
        assert error.value.error == "source_review_target_busy_retry"
    assert answer(sql_scope, candidate_id, "keep_current").data.resolved


def test_distinct_scopes_in_one_batch_share_transaction_but_not_review(sql_scope, monkeypatch):
    seed(sql_scope, monkeypatch)
    seed(sql_scope, monkeypatch, scope=PROJECT)
    quote = "I haven't decided which defaults to keep."
    requests = [request(scope_ref=scope_ref, quote=quote) for scope_ref in ("global", "cedar")]
    result, job = apply(sql_scope, requests=requests)
    assert result.withheld == result.buffered == 2
    with sql_scope.factory() as session:
        reviews = session.scalars(select(PendingExtractionCandidate).where(
            PendingExtractionCandidate.extraction_job_id == job)).all()
        assert len({row.candidate_fingerprint for row in reviews}) == 2
    assert read_decisions(sql_scope, PROJECT)[0] == read_decisions(sql_scope, {"kind": "global"})[0] == []
