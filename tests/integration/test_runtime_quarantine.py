"""Audited quarantine with real SQL/HTTP; no provider or currentness shortcut."""
import asyncio
import copy
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import event, select

from api.db.models import (
    AuditLog,
    ExtractionJob,
    ExtractionJobStatus,
    ProxyUser,
    RuntimeJobReconciliation,
)
from api.settings import get_settings
from api.tasks import provenance_tasks
from tests.integration.test_preference_fact_transaction import fact
from tests.integration.test_runtime_pending_visibility import read
from tests.integration.test_runtime_recovery import (
    forbid_real_providers as _forbid,
)
from tests.integration.test_runtime_recovery import (
    ingestion as _ingestion,
)
from tests.integration.test_runtime_recovery import (
    prepare,
    saved,
)
from tests.integration.test_runtime_recovery import (
    recovery_api as _api,
)
from tests.integration.test_runtime_recovery import (
    sql_scope as _scope,
)

sql_scope, ingestion, recovery_api, forbid_real_providers = _scope, _ingestion, _api, _forbid
REASON = "Raw evidence expired; preserve the unresolved hold"


def reconcile(api, job_id, *, reason=REASON, authorized=True):
    async def run():
        async with api.client() as http:
            return await http.post(f"/v1/internal/dead-letter-jobs/{job_id}/reconcile",
                params={} if reason is None else {"reason": reason},
                headers={} if authorized else {"X-Admin-Secret": "not-authorized"})
    return asyncio.run(run())


def expire(scope, job_id):
    with scope.factory.begin() as session:
        session.get(ExtractionJob, job_id).raw_payload_expires_at = datetime.now(UTC) - timedelta(seconds=1)


def audits(scope):
    with scope.factory() as session:
        return list(session.scalars(select(AuditLog).where(AuditLog.proxy_user_id == scope.proxy_id,
            AuditLog.metadata_json["event"].astext == "runtime_job_quarantined")))


@pytest.mark.parametrize("redact", [False, True])
def test_expired_bound_evidence_quarantines_without_restoring_currentness(
    sql_scope, ingestion, monkeypatch, recovery_api, redact,
):
    old, job_id, _, model, embedder = prepare(sql_scope, ingestion, monkeypatch, "validated")
    unrelated = fact(sql_scope, "my city is Pune")
    monkeypatch.setattr(get_settings(), "runtime_target_withholding_enabled", True)
    expire(sql_scope, job_id)
    if redact:
        monkeypatch.setattr(provenance_tasks, "build_sync_session_factory", lambda: sql_scope.factory)
        assert provenance_tasks.redact_expired_extraction_payloads.run()["redacted_jobs"] == 1
    before = saved(sql_scope, job_id)
    response = reconcile(recovery_api, job_id)
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["outcome"] == "quarantined" and data["hold_scope"] == "affected_context"
    assert data["footprint_sha256"] == before["footprint"]["projection_sha256"]
    assert data["reason_code"] == ("source_redacted" if redact else "source_expired")
    assert data["actor_credential_class"] == "admin_secret" and data["request_id"] == "recovery-check"
    assert saved(sql_scope, job_id) == before
    assert len(audits(sql_scope)) == 1 and str(audits(sql_scope)[0].id) == data["audit_id"]
    items, visibility, decisions = read(sql_scope, [old, unrelated])
    assert [m.id for m in items] == [str(unrelated)] and not decisions
    assert visibility.state == "unavailable" and visibility.scope == "affected_context"
    retry = recovery_api.call(job_id)
    assert retry.status_code == 409 and retry.json()["detail"] == "runtime_job_quarantined"
    assert recovery_api.call(job_id, method="DELETE").status_code == 409
    assert not recovery_api.dispatched and not model.calls and not embedder.calls


@pytest.mark.parametrize("change", ["uncaptured", "record", "source"])
def test_unknown_or_changed_evidence_quarantine_remains_user_wide(
    sql_scope, ingestion, monkeypatch, recovery_api, change,
):
    old, job_id, _, _, _ = prepare(sql_scope, ingestion, monkeypatch,
        "uncaptured" if change == "uncaptured" else "validated")
    monkeypatch.setattr(get_settings(), "runtime_target_withholding_enabled", True)
    if change == "uncaptured":
        expire(sql_scope, job_id)
    else:
        with sql_scope.factory.begin() as session:
            job = session.get(ExtractionJob, job_id)
            if change == "record":
                raw = copy.deepcopy(job.preference_intent_admission)
                raw["catalog_sha256"] = "0" * 64
                job.preference_intent_admission = raw
            else:
                job.payload = {**job.payload, "messages": [{"role": "user", "content": "changed"}]}
    response = reconcile(recovery_api, job_id)
    assert response.status_code == 200, response.text
    assert response.json()["data"]["hold_scope"] == "user_learned_context"
    assert response.json()["data"]["footprint_sha256"] is None
    items, visibility, _ = read(sql_scope, [old])
    assert not items and visibility.scope == "user_learned_context" and visibility.state == "unavailable"


@pytest.mark.parametrize("rollback", [False, True])
def test_provider_or_flag_failure_is_not_irrecoverable_evidence(
    sql_scope, ingestion, monkeypatch, recovery_api, rollback,
):
    _, job_id, _, model, embedder = prepare(sql_scope, ingestion, monkeypatch, "validated")
    if rollback:
        monkeypatch.setattr(get_settings(), "runtime_preference_enabled", False)
    response = reconcile(recovery_api, job_id)
    assert response.status_code == 409 and response.json()["detail"] == "runtime_job_recoverable"
    assert not audits(sql_scope) and not recovery_api.dispatched and not model.calls and not embedder.calls


def test_same_reason_is_idempotent_different_reason_cannot_rewrite_audit(
    sql_scope, ingestion, monkeypatch, recovery_api,
):
    _, job_id, _, _, _ = prepare(sql_scope, ingestion, monkeypatch, "validated")
    expire(sql_scope, job_id)
    first, second = reconcile(recovery_api, job_id), reconcile(recovery_api, job_id)
    assert first.status_code == second.status_code == 200
    assert first.json()["data"] == second.json()["data"] and len(audits(sql_scope)) == 1
    assert reconcile(recovery_api, job_id, reason="changed reason").status_code == 409


def test_concurrent_quarantine_creates_one_audit(sql_scope, ingestion, monkeypatch, recovery_api):
    _, job_id, _, _, _ = prepare(sql_scope, ingestion, monkeypatch, "validated")
    expire(sql_scope, job_id)
    async def run():
        async with recovery_api.client() as http:
            return await asyncio.gather(*(http.post(f"/v1/internal/dead-letter-jobs/{job_id}/reconcile",
                params={"reason": REASON}) for _ in range(2)))
    responses = asyncio.run(run())
    assert all(r.status_code in {200, 409} for r in responses), [r.text for r in responses]
    assert any(r.status_code == 200 for r in responses) and len(audits(sql_scope)) == 1
    assert not recovery_api.dispatched


def test_audit_failure_rolls_back_quarantine_without_releasing_hold(sql_scope, ingestion, monkeypatch, recovery_api):
    old, job_id, _, _, _ = prepare(sql_scope, ingestion, monkeypatch, "validated")
    expire(sql_scope, job_id)
    before = saved(sql_scope, job_id)
    def reject(_mapper, _connection, audit):
        if audit.metadata_json.get("event") == "runtime_job_quarantined":
            raise RuntimeError("controlled audit persistence failure")
    event.listen(AuditLog, "before_insert", reject)
    try:
        assert reconcile(recovery_api, job_id).status_code == 500
    finally:
        event.remove(AuditLog, "before_insert", reject)
    with sql_scope.factory() as session:
        assert session.get(RuntimeJobReconciliation, job_id) is None
        assert session.get(ExtractionJob, job_id).status == ExtractionJobStatus.dead
    assert saved(sql_scope, job_id) == before and not audits(sql_scope)
    assert read(sql_scope, [old])[0] == [] and not recovery_api.dispatched


def test_auth_reason_owner_applied_and_lock_gates(sql_scope, ingestion, monkeypatch, recovery_api):
    _, job_id, _, _, _ = prepare(sql_scope, ingestion, monkeypatch, "validated")
    expire(sql_scope, job_id)
    assert reconcile(recovery_api, job_id, authorized=False).status_code == 403
    for reason in (None, "   ", "x" * 513):
        assert reconcile(recovery_api, job_id, reason=reason).status_code == 422
    assert reconcile(recovery_api, uuid.uuid4()).status_code == 404
    with sql_scope.factory.begin() as session:
        session.get(ExtractionJob, job_id, with_for_update=True)
        response = reconcile(recovery_api, job_id)
        assert response.status_code == 409 and response.json()["detail"] == "dead_letter_job_busy"
    with sql_scope.factory.begin() as session:
        session.get(ProxyUser, sql_scope.proxy_id).is_blocked = True
    assert reconcile(recovery_api, job_id).status_code == 409
    with sql_scope.factory.begin() as session:
        session.get(ProxyUser, sql_scope.proxy_id).is_blocked = False
        session.get(ExtractionJob, job_id).runtime_applied_at = datetime.now(UTC)
    assert reconcile(recovery_api, job_id).status_code == 409
    assert not audits(sql_scope) and not recovery_api.dispatched


@pytest.mark.parametrize("change", ["digest", "owner", "extra", "audit_deleted"])
def test_reconciliation_integrity_and_audit_retention_never_release_guard(
    sql_scope, ingestion, monkeypatch, recovery_api, change,
):
    old, job_id, _, _, _ = prepare(sql_scope, ingestion, monkeypatch, "validated")
    unrelated = fact(sql_scope, "my city is Pune")
    monkeypatch.setattr(get_settings(), "runtime_target_withholding_enabled", True)
    expire(sql_scope, job_id)
    assert reconcile(recovery_api, job_id).status_code == 200
    with sql_scope.factory.begin() as session:
        row = session.get(RuntimeJobReconciliation, job_id)
        if change == "audit_deleted":
            session.delete(session.get(AuditLog, row.audit_id))
        elif change == "owner":
            row.tenant_id = uuid.uuid4()
        else:
            row.record_json = {**row.record_json, **({"footprint_sha256": "0" * 64}
                if change == "digest" else {"authority": 100})}
    items, visibility, _ = read(sql_scope, [old, unrelated])
    assert str(old) not in {m.id for m in items} and visibility.state == "unavailable"
    assert visibility.scope == ("affected_context" if change == "audit_deleted" else "user_learned_context")
    assert recovery_api.call(job_id).status_code == 409
