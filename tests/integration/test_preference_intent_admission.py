"""Disposable PostgreSQL retry/transaction checks, never a model-quality eval."""
import asyncio
import os
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import sessionmaker

from api.db.database import get_sync_database_url
from api.db.models import (
    Conversation,
    ExtractionJob,
    ExtractionJobStatus,
    Memory,
    PendingExtractionCandidate,
    ProxyUser,
    User,
)
from api.infra import protected_storage
from api.services.memory_service import MemoryService
from api.services.preference_intent_admission_service import (
    PreferenceAdmissionUnavailable,
    PreferenceIntentAdmissionService,
)
from api.settings import get_settings
from api.tasks import extraction_tasks, provenance_tasks
from tests.integration.test_preference_choice_operations import apply as direct_apply
from tests.integration.test_preference_choice_operations import counts, read, review_id
from tests.integration.test_preference_snapshots import identity, offered, seed
from tests.integration.test_source_decision_postgres import sql_scope as _sql_scope
from tests.unit.test_preference_choice_service import request
from tests.unit.test_protected_storage import FakeCipher

sql_scope = _sql_scope


@pytest.fixture(autouse=True)
def forbid_real_providers(monkeypatch):
    monkeypatch.setattr(get_settings(), "explicit_preference_claims_enabled", True)
    monkeypatch.setattr(protected_storage, "_active_cipher", lambda: None)
    def forbidden(*_args, **_kwargs):
        pytest.fail("durable admission must not call a provider")
    for method in ("complete", "complete_sync"):
        monkeypatch.setattr(f"api.services.llm_service.LLMService.{method}", forbidden)
    for method in ("embed", "embed_sync"):
        monkeypatch.setattr(f"api.services.embedding_service.EmbeddingService.{method}", forbidden)


def job(scope, *, messages=None, **payload_changes):
    job_id = uuid.uuid4()
    payload = {"messages": messages if messages is not None else [{"role": "user", "content": request()["quote"]}],
        "tenant_id": str(scope.tenant_id), "proxy_user_id": str(scope.proxy_id), **payload_changes}
    with scope.factory.begin() as session:
        session.add(ExtractionJob(id=job_id, tenant_id=scope.tenant_id, proxy_user_id=scope.proxy_id,
            external_user_id=scope.external_id, payload=payload))
    return job_id


def args(scope, job_id, **changes):
    return dict(job_id=job_id, tenant_id=scope.tenant_id, proxy_user_id=scope.proxy_id,
        catalog=offered(scope), **changes)


def prepare(scope, *, requests=None):
    job_id, service = job(scope), PreferenceIntentAdmissionService(scope.factory)
    captured = service.capture(**args(scope, job_id))
    validated = service.admit([request()] if requests is None else requests,
        **args(scope, job_id), capture_id=captured.capture_id)
    return service, job_id, validated


def stored(scope, job_id):
    with scope.factory() as session:
        return PreferenceIntentAdmissionService._read(session.get(ExtractionJob, job_id))


def conversation_count(scope):
    with scope.factory() as session:
        return session.scalar(select(func.count()).select_from(Conversation).join(User).where(
            User.external_id == f"proxy::{scope.proxy_id}"))


def test_capture_once_and_preserve_original_binding(sql_scope, monkeypatch):
    target = seed(sql_scope, monkeypatch)
    job_id, service = job(sql_scope), PreferenceIntentAdmissionService(sql_scope.factory)
    before = conversation_count(sql_scope)
    with ThreadPoolExecutor(max_workers=2) as pool:
        captured = list(pool.map(lambda _: service.capture(**args(sql_scope, job_id)), range(2)))
    assert captured[0] == captured[1] == stored(sql_scope, job_id)
    assert conversation_count(sql_scope) == before + 1
    original = captured[0]
    with sql_scope.factory.begin() as session:
        memory = session.get(Memory, target)
        memory.metadata_json = {**memory.metadata_json, "later_edit": True}
    assert service.capture(**args(sql_scope, job_id)) == original
    assert identity(original.snapshot).target.memory_version != identity(read(sql_scope)).target.memory_version


def test_capture_uses_one_connection_even_with_a_one_slot_pool(sql_scope, monkeypatch):
    seed(sql_scope, monkeypatch)
    job_id = job(sql_scope)
    engine = create_engine(get_sync_database_url(), pool_size=1, max_overflow=0, pool_timeout=1)
    try:
        service = PreferenceIntentAdmissionService(sessionmaker(engine, expire_on_commit=False))
        assert service.capture(**args(sql_scope, job_id)).state == "captured"
    finally:
        engine.dispose()


def test_capture_failure_rolls_back_new_conversation_and_backing_user(sql_scope, monkeypatch):
    job_id = job(sql_scope)
    service = PreferenceIntentAdmissionService(sql_scope.factory)
    def crash(*_args, **_kwargs):
        raise RuntimeError("injected capture encryption failure")
    with monkeypatch.context() as patch:
        patch.setattr("api.services.preference_intent_admission_service.encrypt_json_for_dual_write", crash)
        with pytest.raises(RuntimeError, match="injected"):
            service.capture(**args(sql_scope, job_id))
    assert stored(sql_scope, job_id) is None and conversation_count(sql_scope) == 0
    with sql_scope.factory() as session:
        assert session.scalars(select(User).where(User.external_id == f"proxy::{sql_scope.proxy_id}")).first() is None
    assert service.capture(**args(sql_scope, job_id)).state == "captured"


def test_concurrent_different_decisions_cannot_overwrite_each_other(sql_scope, monkeypatch):
    seed(sql_scope, monkeypatch)
    job_id, service = job(sql_scope), PreferenceIntentAdmissionService(sql_scope.factory)
    record = service.capture(**args(sql_scope, job_id))
    def admit(requests):
        try:
            return service.admit(requests, **args(sql_scope, job_id), capture_id=record.capture_id)
        except PreferenceAdmissionUnavailable as exc:
            assert str(exc) == "admission_decision_already_fixed"
            return None
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(admit, ([request()], [])))
    assert sum(result is not None for result in results) == 1
    assert stored(sql_scope, job_id) == next(result for result in results if result is not None)


def test_fresh_profile_and_empty_decision_do_not_create_preferences(sql_scope):
    before = counts(sql_scope)
    service, job_id, record = prepare(sql_scope, requests=[])
    assert all(item.target is None for item in record.snapshot.identities)
    result = service.apply(**args(sql_scope, job_id), capture_id=record.capture_id)
    assert result.state == "applied" and result.outcome.withheld == 0 and result.requests == ()
    assert counts(sql_scope) == before
    assert service.apply(**args(sql_scope, job_id), capture_id=record.capture_id) == result


def test_first_validated_decision_survives_process_restart(sql_scope, monkeypatch):
    seed(sql_scope, monkeypatch)
    service, job_id, record = prepare(sql_scope)
    restarted = PreferenceIntentAdmissionService(sql_scope.factory)
    assert restarted.capture(**args(sql_scope, job_id)) == record
    assert restarted.admit([request()], **args(sql_scope, job_id), capture_id=record.capture_id) == record
    with pytest.raises(PreferenceAdmissionUnavailable, match="already_fixed"):
        service.admit([], **args(sql_scope, job_id), capture_id=record.capture_id)
    assert stored(sql_scope, job_id) == record
    result = restarted.apply(**args(sql_scope, job_id), capture_id=record.capture_id)
    assert result.outcome.withheld == result.outcome.reviews_buffered == 1


def test_concurrent_application_has_one_audit_and_one_review(sql_scope, monkeypatch):
    seed(sql_scope, monkeypatch)
    service, job_id, record = prepare(sql_scope)
    before = counts(sql_scope)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: service.apply(**args(sql_scope, job_id), capture_id=record.capture_id), range(2)))
    assert results[0] == results[1] == stored(sql_scope, job_id)
    assert results[0].outcome.withheld == 1
    assert counts(sql_scope) == (*before[:3], before[3] + 1)
    with sql_scope.factory() as session:
        assert session.scalar(select(func.count()).select_from(PendingExtractionCandidate).where(
            PendingExtractionCandidate.extraction_job_id == job_id)) == 1


@pytest.mark.parametrize("failure", ["review", "encryption"])
def test_apply_failure_rolls_back_choice_audit_review_and_receipt(sql_scope, monkeypatch, failure):
    seed(sql_scope, monkeypatch)
    service, job_id, record = prepare(sql_scope)
    before, count_before = read(sql_scope), counts(sql_scope)
    def crash(*_args, **_kwargs):
        raise RuntimeError("injected persistence failure")
    with monkeypatch.context() as patch:
        if failure == "review":
            patch.setattr(extraction_tasks, "_persist_pending_extraction_candidates", crash)
        else:
            patch.setattr("api.services.preference_intent_admission_service.encrypt_json_for_dual_write", crash)
        with pytest.raises(RuntimeError, match="injected"):
            service.apply(**args(sql_scope, job_id), capture_id=record.capture_id)
    assert stored(sql_scope, job_id) == record
    assert read(sql_scope) == before and counts(sql_scope) == count_before
    with sql_scope.factory() as session:
        assert session.scalar(select(func.count()).select_from(PendingExtractionCandidate).where(
            PendingExtractionCandidate.extraction_job_id == job_id)) == 0
    assert service.apply(**args(sql_scope, job_id), capture_id=record.capture_id).outcome.withheld == 1


def test_completed_result_overwrite_cannot_lose_receipt_or_restore_old_hold(sql_scope, monkeypatch):
    seed(sql_scope, monkeypatch)
    service, job_id, record = prepare(sql_scope)
    applied = service.apply(**args(sql_scope, job_id), capture_id=record.capture_id)
    monkeypatch.setattr(extraction_tasks, "build_extraction_session_factory", lambda: sql_scope.factory)
    extraction_tasks._set_db_job_completed(job_id=str(job_id), payload={"memories_created": 0, "stored_memories": []})
    assert stored(sql_scope, job_id) == applied
    direct_apply(sql_scope, requests=[request(action="reaffirm")])
    now, before = read(sql_scope), counts(sql_scope)
    assert identity(now).target.choice_state == "current"
    assert service.apply(**args(sql_scope, job_id), capture_id=record.capture_id) == applied
    assert read(sql_scope) == now and counts(sql_scope) == before
    assert review_id(sql_scope, job_id)


def test_changed_target_is_durably_rejected_not_rebound(sql_scope, monkeypatch):
    target = seed(sql_scope, monkeypatch)
    service, job_id, record = prepare(sql_scope)
    with sql_scope.factory.begin() as session:
        memory = session.get(Memory, target)
        memory.metadata_json = {**memory.metadata_json, "edit_after_admission": True}
    before = counts(sql_scope)
    result = service.apply(**args(sql_scope, job_id), capture_id=record.capture_id)
    assert result.state == "rejected" and result.outcome.rejected == {"preference_target_stale": 1}
    assert counts(sql_scope) == before and identity(read(sql_scope)).target.choice_state == "current"
    assert service.apply(**args(sql_scope, job_id), capture_id=record.capture_id) == result


@pytest.mark.parametrize("status", [ExtractionJobStatus.completed, ExtractionJobStatus.dead])
@pytest.mark.parametrize("stage", ["capture", "admit", "apply"])
def test_closed_job_cannot_start_or_apply_unfinished_admission(sql_scope, monkeypatch, status, stage):
    seed(sql_scope, monkeypatch)
    job_id, service = job(sql_scope), PreferenceIntentAdmissionService(sql_scope.factory)
    captured = None
    if stage != "capture":
        captured = service.capture(**args(sql_scope, job_id))
    if stage == "apply":
        captured = service.admit([request()], **args(sql_scope, job_id), capture_id=captured.capture_id)
    with sql_scope.factory.begin() as session:
        session.get(ExtractionJob, job_id).status = status
    before = counts(sql_scope)
    with pytest.raises(PreferenceAdmissionUnavailable):
        if stage == "capture":
            service.capture(**args(sql_scope, job_id))
        elif stage == "admit":
            service.admit([request()], **args(sql_scope, job_id), capture_id=captured.capture_id)
        else:
            service.apply(**args(sql_scope, job_id), capture_id=captured.capture_id)
    assert stored(sql_scope, job_id) == captured and counts(sql_scope) == before


def test_admitted_reaffirmation_preserves_the_original_value_and_authority(sql_scope, monkeypatch):
    seed(sql_scope, monkeypatch)
    direct_apply(sql_scope)
    quote = "Keep my stored default."
    job_id = job(sql_scope, messages=[{"role": "user", "content": quote}])
    service = PreferenceIntentAdmissionService(sql_scope.factory)
    captured = service.capture(**args(sql_scope, job_id))
    service.admit([request(action="reaffirm", quote=quote)], **args(sql_scope, job_id), capture_id=captured.capture_id)
    result = service.apply(**args(sql_scope, job_id), capture_id=captured.capture_id)
    target = identity(read(sql_scope)).target
    assert result.outcome.reaffirmed == 1 and result.outcome.withheld == result.outcome.reviews_buffered == 0
    assert target.memory_id == identity(captured.snapshot).target.memory_id
    assert target.value == "C++" and target.authority == 20 and target.choice_state == "current"


@pytest.mark.parametrize("change", ["quote", "role", "source_kind", "turn_id", "qualifier", "catalog", "capture_id"])
def test_changed_inputs_do_not_recapture_or_apply(sql_scope, monkeypatch, change):
    seed(sql_scope, monkeypatch)
    service, job_id, record = prepare(sql_scope)
    call_args = args(sql_scope, job_id)
    capture_id = record.capture_id
    if change == "catalog":
        raw = call_args["catalog"].model_dump(mode="json")
        raw["properties"][0]["description"] = "Changed meaning"
        call_args["catalog"] = type(call_args["catalog"]).model_validate(raw)
    elif change == "capture_id":
        capture_id = uuid.uuid4()
    else:
        with sql_scope.factory.begin() as session:
            row = session.get(ExtractionJob, job_id)
            turn = {**row.payload["messages"][0]}
            turn.update({"quote": {"content": "Different text"}, "role": {"role": "tool"},
                "source_kind": {"source_kind": "tool_result"}, "turn_id": {"turn_id": "different"},
                "qualifier": {"content": turn["content"] + " For another project only."}}[change])
            row.payload = {**row.payload, "messages": [turn]}
    before = conversation_count(sql_scope), counts(sql_scope)
    with pytest.raises(PreferenceAdmissionUnavailable, match="inputs_changed"):
        service.apply(**call_args, capture_id=capture_id)
    assert stored(sql_scope, job_id) == record
    assert (conversation_count(sql_scope), counts(sql_scope)) == before


@pytest.mark.parametrize("change", ["tenant", "profile", "blocked", "inactive", "source_owner", "missing_source", "disabled"])
def test_unavailable_owner_source_or_policy_fails_closed(sql_scope, monkeypatch, change):
    seed(sql_scope, monkeypatch)
    service, job_id, record = prepare(sql_scope)
    call_args = args(sql_scope, job_id)
    if change == "tenant":
        call_args["tenant_id"] = uuid.uuid4()
    elif change == "profile":
        call_args["proxy_user_id"] = uuid.uuid4()
    elif change == "disabled":
        monkeypatch.setattr(get_settings(), "explicit_preference_claims_enabled", False)
    else:
        with sql_scope.factory.begin() as session:
            backing = session.scalars(select(User).where(User.external_id == f"proxy::{sql_scope.proxy_id}")).one()
            if change == "blocked":
                session.get(ProxyUser, sql_scope.proxy_id).is_blocked = True
            elif change == "inactive":
                backing.is_active = False
            elif change == "missing_source":
                session.delete(session.get(Conversation, record.source_conversation_id))
            else:
                # An unrelated real user cannot become the evidence owner.
                other_proxy_id = uuid.uuid4()
                other = User(id=uuid.uuid4(), external_id=f"proxy::{other_proxy_id}",
                    email=f"test-{uuid.uuid4()}@example.invalid", settings={}, is_active=True)
                session.add(other)
                session.flush()
                session.get(Conversation, record.source_conversation_id).user_id = other.id
                # Fixture cleanup owns this synthetic user too.
                sql_scope.proxy_ids.append(other_proxy_id)
    before = counts(sql_scope)
    with pytest.raises(PreferenceAdmissionUnavailable):
        service.apply(**call_args, capture_id=record.capture_id)
    assert stored(sql_scope, job_id) == record and counts(sql_scope) == before


@pytest.mark.parametrize("kind", ["forged_quote", "tool", "duplicate"])
def test_invalid_decision_cannot_fix_or_mutate_capture(sql_scope, monkeypatch, kind):
    seed(sql_scope, monkeypatch)
    job_id = job(sql_scope, messages=[{"role": "tool" if kind == "tool" else "user", "content": request()["quote"]}])
    service = PreferenceIntentAdmissionService(sql_scope.factory)
    captured = service.capture(**args(sql_scope, job_id))
    requests = [request(quote="Made up user evidence")] if kind == "forged_quote" else [request()] * (2 if kind == "duplicate" else 1)
    with pytest.raises(ValueError):
        service.admit(requests, **args(sql_scope, job_id), capture_id=captured.capture_id)
    assert stored(sql_scope, job_id) == captured
    with pytest.raises(PreferenceAdmissionUnavailable, match="decision_missing"):
        service.apply(**args(sql_scope, job_id), capture_id=captured.capture_id)


@pytest.mark.parametrize("payload", [
    {"agent_id": str(uuid.uuid4())}, {"source": {"explicit": True}}, {"source": "not-an-object"},
    {"preference_claims": []}, {"tenant_id": str(uuid.uuid4())},
    {"messages": [{"role": "user", "content": "x" * 65536}]},
    {"messages": [{"role": "user", "content": "small"}] * 65},
])
def test_ineligible_or_oversized_source_never_creates_capture(sql_scope, payload):
    job_id = job(sql_scope, **payload)
    service = PreferenceIntentAdmissionService(sql_scope.factory)
    before = conversation_count(sql_scope)
    with pytest.raises(PreferenceAdmissionUnavailable):
        service.capture(**args(sql_scope, job_id))
    assert stored(sql_scope, job_id) is None and conversation_count(sql_scope) == before


def test_retention_erases_plaintext_and_envelope_and_prevents_recapture(sql_scope, monkeypatch):
    seed(sql_scope, monkeypatch)
    cipher = FakeCipher()
    monkeypatch.setattr(protected_storage, "_active_cipher", lambda: cipher)
    service, job_id, record = prepare(sql_scope)
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
        service.capture(**args(sql_scope, job_id))
    with pytest.raises(PreferenceAdmissionUnavailable, match="source_unavailable"):
        service.apply(**args(sql_scope, job_id), capture_id=record.capture_id)


@pytest.mark.parametrize("raw", [{"malformed": True}, {"oversized": "x" * 262145}, []])
def test_database_bound_and_fail_closed_record(sql_scope, raw):
    job_id = job(sql_scope)
    if "malformed" not in raw:
        with pytest.raises(IntegrityError), sql_scope.factory.begin() as session:
            session.get(ExtractionJob, job_id).preference_intent_admission = raw
        assert stored(sql_scope, job_id) is None
    else:
        with sql_scope.factory.begin() as session:
            session.get(ExtractionJob, job_id).preference_intent_admission = raw
        before = conversation_count(sql_scope)
        with pytest.raises(PreferenceAdmissionUnavailable, match="record_inconsistent"):
            PreferenceIntentAdmissionService(sql_scope.factory).capture(**args(sql_scope, job_id))
        assert conversation_count(sql_scope) == before


def test_public_job_status_does_not_expose_private_admission(sql_scope, monkeypatch):
    seed(sql_scope, monkeypatch)
    _, job_id, _ = prepare(sql_scope)
    async def exercise():
        engine = create_async_engine(os.environ["DATABASE_URL"])
        try:
            async with async_sessionmaker(engine)() as session:
                # Method uses only its session and pure error-summary helper on this DB path.
                service = SimpleNamespace(session=session, _job_error_summary=MemoryService._job_error_summary)
                return await MemoryService.get_job_status(service, job_id=str(job_id))
        finally:
            await engine.dispose()
    status = asyncio.run(exercise())
    assert status["job_id"] == str(job_id)
    assert "preference_intent_admission" not in status and "preference_intent_admission_envelope" not in status
    assert request()["quote"] not in str(status) and "C++" not in str(status)
