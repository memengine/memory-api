"""Disposable SQL atomic writes/retries, with every real provider forbidden."""
import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from api.db.database import get_sync_database_url
from api.db.models import (
    Conversation,
    ExtractionJob,
    ExtractionJobStatus,
    Memory,
    MemoryClaim,
    PendingExtractionCandidate,
    ProxyUser,
)
from api.infra import protected_storage
from api.services.preference_intent_admission_service import (
    PreferenceAdmissionUnavailable,
    PreferenceIntentAdmissionService,
)
from api.services.preference_intent_transaction import (
    PreferenceIntentTransactionService,
)
from api.settings import get_settings
from api.tasks import extraction_tasks
from tests.integration.test_preference_choice_operations import counts, read
from tests.integration.test_preference_intent_admission import (
    args,
    conversation_count,
    job,
)
from tests.integration.test_preference_snapshots import identity, seed
from tests.integration.test_source_decision_postgres import _embedding
from tests.integration.test_source_decision_postgres import sql_scope as _sql_scope
from tests.unit.test_preference_intent_contract import intent, reply

sql_scope = _sql_scope
TEXT = "Use Python instead of C++, and my exam moved to October 18."
FACT = {"category": "fact", "content": "my exam moved to October 18", "quote": "my exam moved to October 18", "turn_index": 0}


@pytest.fixture(autouse=True)
def forbid_real_providers(monkeypatch):
    monkeypatch.setattr(get_settings(), "explicit_preference_claims_enabled", True)
    monkeypatch.setattr(protected_storage, "_active_cipher", lambda: None)
    def forbidden(*_args, **_kwargs):
        pytest.fail("transaction stage must not call a provider")
    for method in ("complete", "complete_sync"):
        monkeypatch.setattr(f"api.services.llm_service.LLMService.{method}", forbidden)
    for method in ("embed", "embed_sync", "_embed_with_model_sync"):
        monkeypatch.setattr(f"api.services.embedding_service.EmbeddingService.{method}", forbidden)


def prepare(scope, raw=None, *, text=TEXT, service=None):
    service = service or PreferenceIntentTransactionService(scope.factory)
    job_id = job(scope, messages=[{"role": "user", "content": text}], external_conversation_id="intent-transaction-chat")
    captured = service.capture(**args(scope, job_id))
    admitted = service.admit(raw or reply(nonpreferences=[FACT]), **args(scope, job_id), capture_id=captured.capture_id)
    return service, job_id, admitted


def apply(scope, service, job_id, record, **changes):
    embeddings = {p.quote: _embedding(p.quote) for p in record.reply.preferences if p.action in {"assert", "set_current"}}
    return service.apply(**args(scope, job_id), capture_id=record.capture_id, embeddings=embeddings, **changes)


def stored(scope, job_id):
    with scope.factory() as session:
        return PreferenceIntentTransactionService._read(session.get(ExtractionJob, job_id))


def pending(scope, job_id):
    with scope.factory() as session:
        return list(session.scalars(select(PendingExtractionCandidate).where(PendingExtractionCandidate.extraction_job_id == job_id)))


def test_mixed_preference_correction_and_fact_are_atomic_and_retry_stable(sql_scope, monkeypatch):
    target = seed(sql_scope, monkeypatch)
    service, job_id, admitted = prepare(sql_scope)
    before = counts(sql_scope)
    result = apply(sql_scope, service, job_id, admitted)
    assert result.state == "applied" and len(result.receipt.created_memory_ids) == 1
    assert result.receipt.deferred_facts == result.receipt.pending_candidates_buffered == 1
    assert identity(read(sql_scope)).target.value == "Python"
    with sql_scope.factory() as session:
        assert session.get(Memory, target).is_archived
        new = session.get(Memory, result.receipt.created_memory_ids[0])
        assert new.metadata_json["provenance"]["attestation"] == "client_asserted"
        assert new.metadata_json["provenance"]["external_conversation_id"] == "intent-transaction-chat"
        assert session.get(Conversation, admitted.source_conversation_id).processing_status.value == "done"
    rows = pending(sql_scope, job_id)
    assert len(rows) == 1 and rows[0].content == FACT["content"] and rows[0].status == "pending"
    assert rows[0].metadata_json["extraction_evidence"]["source_review"]["kind"] == "restate_source"
    assert counts(sql_scope) != before
    after = counts(sql_scope), read(sql_scope), conversation_count(sql_scope), rows[0].reinforcement_count
    restarted = PreferenceIntentTransactionService(sql_scope.factory)
    assert restarted.apply(**args(sql_scope, job_id), capture_id=admitted.capture_id) == result == stored(sql_scope, job_id)
    assert (counts(sql_scope), read(sql_scope), conversation_count(sql_scope), pending(sql_scope, job_id)[0].reinforcement_count) == after


@pytest.mark.parametrize("failure", ["reviews", "encryption", "outbox", "before_commit"])
def test_failure_rolls_back_writes_audits_reviews_and_receipt(sql_scope, monkeypatch, failure):
    from sqlalchemy import event

    seed(sql_scope, monkeypatch)
    service, job_id, record = prepare(sql_scope)
    before = counts(sql_scope), read(sql_scope)
    def crash(*_args, **_kwargs):
        raise RuntimeError("injected transaction failure")
    with monkeypatch.context() as patch:
        if failure == "reviews":
            patch.setattr(extraction_tasks, "_persist_pending_extraction_candidates", crash)
        elif failure == "encryption":
            patch.setattr("api.services.preference_intent_admission_service.encrypt_json_for_dual_write", crash)
        elif failure == "outbox":
            patch.setattr("api.services.conflict_resolver.enqueue_vector_upsert", crash)
        else:
            event.listen(sql_scope.factory.class_, "before_commit", crash)
        try:
            with pytest.raises(RuntimeError, match="injected"):
                apply(sql_scope, service, job_id, record)
        finally:
            if failure == "before_commit":
                event.remove(sql_scope.factory.class_, "before_commit", crash)
    assert stored(sql_scope, job_id) == record and (counts(sql_scope), read(sql_scope)) == before
    assert pending(sql_scope, job_id) == []
    assert len(apply(sql_scope, service, job_id, record).receipt.created_memory_ids) == 1


def test_concurrent_apply_has_one_write_and_one_receipt(sql_scope, monkeypatch):
    seed(sql_scope, monkeypatch)
    service, job_id, record = prepare(sql_scope)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: apply(sql_scope, service, job_id, record), range(2)))
    assert results[0] == results[1] == stored(sql_scope, job_id)
    assert len(results[0].receipt.created_memory_ids) == len(pending(sql_scope, job_id)) == 1
    assert conversation_count(sql_scope) == 2  # Seed conversation plus exactly one capture.


def test_first_writer_race_cannot_replace_a_new_winner(sql_scope):
    service, first_id, first = prepare(sql_scope, reply(nonpreferences=[]))
    other_quote = "Use Rust instead of C++"
    _, second_id, second = prepare(sql_scope, reply([intent(value="Rust", quote=other_quote)]), text=other_quote)
    assert all(i.target is None for i in first.snapshot.identities)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda item: apply(sql_scope, service, *item), [(first_id, first), (second_id, second)]))
    assert sorted(len(r.receipt.created_memory_ids) for r in results) == [0, 1]
    assert sorted(sum(r.receipt.rejected.values()) for r in results) == [0, 1]
    assert identity(read(sql_scope)).target.value in {"Python", "Rust"}


@pytest.mark.parametrize("change", ["target", "authority", "blocked", "transcript", "catalog", "source_owner", "external_conversation"])
def test_stale_or_unavailable_inputs_never_rebind(sql_scope, monkeypatch, change):
    target = seed(sql_scope, monkeypatch)
    service, job_id, record = prepare(sql_scope)
    call_args = args(sql_scope, job_id)
    if change == "catalog":
        raw = call_args["catalog"].model_dump(mode="json")
        raw["properties"][0]["description"] += " altered"
        call_args["catalog"] = type(call_args["catalog"]).model_validate(raw)
    else:
        with sql_scope.factory.begin() as session:
            if change == "target":
                memory = session.get(Memory, target)
                memory.metadata_json = {**memory.metadata_json, "later_edit": True}
            elif change == "authority":
                session.scalars(select(MemoryClaim).where(MemoryClaim.active_memory_id == target)).one().authority_priority = 80
            elif change == "blocked":
                session.get(ProxyUser, sql_scope.proxy_id).is_blocked = True
            elif change == "source_owner":
                from api.db.models import User
                other_proxy = uuid.uuid4()
                other = User(id=uuid.uuid4(), external_id=f"proxy::{other_proxy}",
                    email=f"{other_proxy}@example.invalid", settings={}, is_active=True)
                session.add(other)
                session.flush()
                session.get(Conversation, record.source_conversation_id).user_id = other.id
                sql_scope.proxy_ids.append(other_proxy)
            elif change == "external_conversation":
                row = session.get(ExtractionJob, job_id)
                row.payload = {**row.payload, "external_conversation_id": "different-source"}
            else:
                row = session.get(ExtractionJob, job_id)
                row.payload = {**row.payload, "messages": [{"role": "user", "content": TEXT + " Another project only."}]}
    before = counts(sql_scope)
    if change in {"target", "authority"}:
        result = apply(sql_scope, service, job_id, record)
        assert not result.receipt.created_memory_ids and sum(result.receipt.rejected.values()) == 1
        assert pending(sql_scope, job_id)[0].content == FACT["content"]
    else:
        with pytest.raises(PreferenceAdmissionUnavailable):
            service.apply(**call_args, capture_id=record.capture_id, embeddings={intent()["quote"]: _embedding("x")})
        assert stored(sql_scope, job_id) == record
    assert counts(sql_scope) == before


def test_withhold_and_reaffirm_never_embed_or_change_the_value(sql_scope, monkeypatch):
    seed(sql_scope, monkeypatch)
    text = "I still cannot decide my default."
    service, job_id, record = prepare(sql_scope, reply([intent(action="withhold", value=None, quote=text)]), text=text)
    held = service.apply(**args(sql_scope, job_id), capture_id=record.capture_id)
    assert held.receipt.withheld == 1 and identity(read(sql_scope)).target.choice_state == "current_choice_unresolved"
    text = "Keep my stored default as current."
    _, job_id, record = prepare(sql_scope, reply([intent(action="reaffirm", value=None, quote=text)]), text=text)
    reaffirmed = service.apply(**args(sql_scope, job_id), capture_id=record.capture_id)
    assert reaffirmed.receipt.reaffirmed == 1
    assert identity(read(sql_scope)).target.value == "C++" and identity(read(sql_scope)).target.choice_state == "current"


def test_new_declaration_and_choice_share_one_atomic_receipt(sql_scope, monkeypatch):
    seed(sql_scope, monkeypatch)
    raw = reply([
        intent(property_key="answer.detail", value="Short", quote="Short answers by default"),
        intent(action="withhold", value=None, quote="I cannot decide the language default"),
    ])
    text = "Short answers by default. I cannot decide the language default."
    service, job_id, record = prepare(sql_scope, raw, text=text)
    before = counts(sql_scope), read(sql_scope)
    with monkeypatch.context() as patch:
        def crash(*_args, **_kwargs):
            raise RuntimeError("injected final receipt failure")
        patch.setattr("api.services.preference_intent_admission_service.encrypt_json_for_dual_write", crash)
        with pytest.raises(RuntimeError, match="injected"):
            apply(sql_scope, service, job_id, record)
    assert stored(sql_scope, job_id) == record and (counts(sql_scope), read(sql_scope)) == before
    assert not pending(sql_scope, job_id)
    result = apply(sql_scope, service, job_id, record)
    assert len(result.receipt.created_memory_ids) == result.receipt.withheld == result.receipt.pending_candidates_buffered == 1
    now = read(sql_scope)
    assert identity(now).target.choice_state == "current_choice_unresolved"
    assert identity(now, key="answer.detail").target.value == "Short"
    after = counts(sql_scope)
    assert service.apply(**args(sql_scope, job_id), capture_id=record.capture_id) == result
    assert counts(sql_scope) == after


def test_tentative_and_unrepresentable_text_remain_nonactivating_reviews(sql_scope):
    for raw in (reply([intent(action="tentative")]), reply([intent(action="abstain", value=None, property_key=None, scope_ref=None)])):
        service, job_id, record = prepare(sql_scope, raw, text=intent()["quote"])
        result = service.apply(**args(sql_scope, job_id), capture_id=record.capture_id)
        assert not result.receipt.created_memory_ids
        assert result.reply.preferences and result.receipt.pending_candidates_buffered == 1
    assert all(i.target is None for i in read(sql_scope).identities)


def test_invalid_evidence_does_not_fix_decision_and_first_decision_wins(sql_scope):
    service, job_id, record = prepare(sql_scope)
    assert PreferenceIntentTransactionService(sql_scope.factory).capture(**args(sql_scope, job_id)) == record
    with pytest.raises(PreferenceAdmissionUnavailable, match="already_fixed"):
        service.admit(reply([], []), **args(sql_scope, job_id), capture_id=record.capture_id)
    other_id = job(sql_scope, messages=[{"role": "tool", "content": TEXT}])
    captured = service.capture(**args(sql_scope, other_id))
    with pytest.raises(ValueError):
        service.admit(reply(), **args(sql_scope, other_id), capture_id=captured.capture_id)
    assert stored(sql_scope, other_id) == captured


def test_missing_embeddings_and_callback_do_not_write(sql_scope):
    service, job_id, record = prepare(sql_scope)
    for embeddings, error in ((None, PreferenceAdmissionUnavailable), (lambda _: pytest.fail("provider callback"), TypeError)):
        with pytest.raises(error):
            service.apply(**args(sql_scope, job_id), capture_id=record.capture_id, embeddings=embeddings)
    assert stored(sql_scope, job_id) == record and pending(sql_scope, job_id) == []


def test_completed_job_result_cannot_erase_receipt(sql_scope, monkeypatch):
    service, job_id, record = prepare(sql_scope)
    result = apply(sql_scope, service, job_id, record)
    monkeypatch.setattr(extraction_tasks, "build_extraction_session_factory", lambda: sql_scope.factory)
    extraction_tasks._set_db_job_completed(job_id=str(job_id), payload={"stored_memories": []})
    assert service.apply(**args(sql_scope, job_id), capture_id=record.capture_id) == result
    with pytest.raises(PreferenceAdmissionUnavailable, match="record_inconsistent"):
        PreferenceIntentAdmissionService(sql_scope.factory).capture(**args(sql_scope, job_id))


@pytest.mark.parametrize("status", [ExtractionJobStatus.completed, ExtractionJobStatus.dead])
def test_closed_unfinished_job_cannot_apply(sql_scope, status):
    service, job_id, record = prepare(sql_scope)
    with sql_scope.factory.begin() as session:
        session.get(ExtractionJob, job_id).status = status
    with pytest.raises(PreferenceAdmissionUnavailable, match="job_closed"):
        apply(sql_scope, service, job_id, record)


def test_one_slot_pool_and_no_hidden_provider(sql_scope):
    engine = create_engine(get_sync_database_url(), pool_size=1, max_overflow=0, pool_timeout=1)
    try:
        service = PreferenceIntentTransactionService(sessionmaker(engine, expire_on_commit=False))
        _, job_id, record = prepare(sql_scope, service=service)
        assert apply(sql_scope, service, job_id, record).state == "applied"
    finally:
        engine.dispose()


def test_protected_authority_at_capture_is_not_downgraded(sql_scope, monkeypatch):
    target = seed(sql_scope, monkeypatch)
    with sql_scope.factory.begin() as session:
        session.scalars(select(MemoryClaim).where(MemoryClaim.active_memory_id == target)).one().authority_priority = 80
    service, job_id, record = prepare(sql_scope)
    assert identity(record.snapshot).target.authority == 80
    result = apply(sql_scope, service, job_id, record)
    assert result.receipt.rejected == {"preference_authority_insufficient": 1}
    assert identity(read(sql_scope)).target.value == "C++" and identity(read(sql_scope)).target.authority == 80


def test_review_capacity_outcome_preserves_the_entire_fixed_reply(sql_scope, monkeypatch):
    monkeypatch.setattr(get_settings(), "source_review_profile_row_limit", 1)
    extra = {"content": "my meeting is tomorrow", "quote": "my meeting is tomorrow", "turn_index": 0, "category": "fact"}
    service, job_id, record = prepare(sql_scope, reply([], [FACT, extra]), text=TEXT + " my meeting is tomorrow")
    result = service.apply(**args(sql_scope, job_id), capture_id=record.capture_id)
    assert result.reply == record.reply
    assert result.receipt.deferred_facts == 2
    assert result.receipt.pending_candidates_buffered == result.receipt.reviews_capacity_rejected == 1
    assert len(pending(sql_scope, job_id)) == 1
    assert service.apply(**args(sql_scope, job_id), capture_id=record.capture_id) == result


def test_conflicting_assertion_remains_review_not_replacement(sql_scope, monkeypatch):
    seed(sql_scope, monkeypatch)
    service, job_id, record = prepare(sql_scope, reply([intent(action="assert")]), text=intent()["quote"])
    result = apply(sql_scope, service, job_id, record)
    assert result.receipt.pending_candidates_buffered == 1 and not result.receipt.created_memory_ids
    assert identity(read(sql_scope)).target.value == "C++"


def test_encrypted_receipt_and_retention_reuse_existing_job_policy(sql_scope, monkeypatch):
    from datetime import UTC, datetime, timedelta

    from api.tasks import provenance_tasks
    from tests.unit.test_protected_storage import FakeCipher
    monkeypatch.setattr(protected_storage, "_active_cipher", lambda: FakeCipher())
    service, job_id, record = prepare(sql_scope)
    result = apply(sql_scope, service, job_id, record)
    with sql_scope.factory.begin() as session:
        row = session.get(ExtractionJob, job_id)
        assert row.preference_intent_admission_envelope is not None
        row.raw_payload_expires_at = datetime.now(UTC) - timedelta(seconds=1)
    monkeypatch.setattr(provenance_tasks, "build_sync_session_factory", lambda: sql_scope.factory)
    assert provenance_tasks.redact_expired_extraction_payloads.run()["redacted_jobs"] == 1
    with sql_scope.factory() as session:
        row = session.get(ExtractionJob, job_id)
        assert row.preference_intent_admission is row.preference_intent_admission_envelope is None
    with pytest.raises(PreferenceAdmissionUnavailable, match="source_unavailable"):
        service.apply(**args(sql_scope, job_id), capture_id=result.capture_id)


def test_public_job_status_does_not_expose_runtime_decision(sql_scope):
    import asyncio
    import os
    from types import SimpleNamespace

    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from api.services.memory_service import MemoryService
    service, job_id, record = prepare(sql_scope)
    apply(sql_scope, service, job_id, record)
    async def get_status():
        engine = create_async_engine(os.environ["DATABASE_URL"])
        try:
            async with async_sessionmaker(engine)() as session:
                reader = SimpleNamespace(session=session, _job_error_summary=MemoryService._job_error_summary)
                return await MemoryService.get_job_status(reader, job_id=str(job_id))
        finally:
            await engine.dispose()
    result = asyncio.run(get_status())
    assert result["job_id"] == str(job_id)
    assert "preference_intent_admission" not in result and "preference_intent_admission_envelope" not in result
    assert intent()["quote"] not in str(result) and FACT["content"] not in str(result)


@pytest.mark.parametrize("invalid", ["foreign_tenant", "foreign_profile", "source_event", "disabled"])
def test_ineligible_job_or_owner_cannot_apply(sql_scope, monkeypatch, invalid):
    service, job_id, record = prepare(sql_scope)
    call_args = args(sql_scope, job_id)
    if invalid == "foreign_tenant":
        call_args["tenant_id"] = uuid.uuid4()
    elif invalid == "foreign_profile":
        call_args["proxy_user_id"] = uuid.uuid4()
    elif invalid == "disabled":
        monkeypatch.setattr(get_settings(), "explicit_preference_claims_enabled", False)
    else:
        with sql_scope.factory.begin() as session:
            row = session.get(ExtractionJob, job_id)
            row.payload = {**row.payload, "source_event_id": str(uuid.uuid4())}
    with pytest.raises(PreferenceAdmissionUnavailable):
        service.apply(**call_args, capture_id=record.capture_id)
    assert stored(sql_scope, job_id) == record and not pending(sql_scope, job_id)
