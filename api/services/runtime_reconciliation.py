"""Quarantine irrecoverable work, atomically audited, without restoring old data."""
import uuid
from datetime import UTC, datetime

from api.db.models import (
    AuditAction,
    AuditLog,
    ExtractionJobStatus,
    ProxyUser,
    RuntimeJobReconciliation,
    Tenant,
)
from api.schemas.runtime_reconciliation import RuntimeReconciliationRecord
from api.services.preference_intent_admission_service import (
    PreferenceAdmissionUnavailable,
)
from api.services.runtime_preference_footprint import RuntimePreferenceFootprint
from api.services.runtime_preference_routing import RUNTIME_ROUTE, has_runtime_guard


def quarantine_runtime_job(session, job, *, reason, request_id, ip_address):
    """Caller holds the job row lock; no classifier, fresh capture or target write."""
    if job.status != ExtractionJobStatus.dead or not has_runtime_guard(job) or job.runtime_applied_at is not None:
        raise PreferenceAdmissionUnavailable("runtime_job_not_quarantinable")
    proxy, tenant = session.get(ProxyUser, job.proxy_user_id), session.get(Tenant, job.tenant_id)
    if (proxy is None or proxy.tenant_id != job.tenant_id or proxy.is_blocked
        or tenant is None or not tenant.is_active):
        raise PreferenceAdmissionUnavailable("admission_owner_unavailable")
    existing = session.get(RuntimeJobReconciliation, job.id)
    if existing is not None:
        if existing.tenant_id != job.tenant_id or existing.proxy_user_id != job.proxy_user_id:
            raise PreferenceAdmissionUnavailable("runtime_reconciliation_owner_changed")
        previous = RuntimeReconciliationRecord.model_validate(existing.record_json)
        if previous.operator_reason != reason:
            raise PreferenceAdmissionUnavailable("runtime_reconciliation_already_fixed")
        return existing
    from api.services.runtime_preference_worker import (
        RuntimePreferenceTransactionService,
    )

    adapter = RuntimePreferenceTransactionService(None)
    code = None
    expired = job.raw_payload_expires_at
    if job.payload_redacted_at is not None:
        code = "source_redacted"
    elif expired is not None and (expired.astimezone(UTC) if expired.tzinfo else expired.replace(tzinfo=UTC)) <= datetime.now(UTC):
        code = "source_expired"
    else:
        try:
            adapter._validate_source(session, job)
        except PreferenceAdmissionUnavailable:
            code = "source_binding_changed"
        else:
            try:
                record = adapter._read(job)
            except PreferenceAdmissionUnavailable:
                code = "record_invalid"
            else:
                if record is not None:
                    try:
                        adapter._check(session, job, (job.payload or {}).get("messages"), record, record.snapshot.catalog)
                    except PreferenceAdmissionUnavailable as exc:
                        code = "record_invalid" if str(exc) == "intent_footprint_changed" else "source_binding_changed"
    if code is None:
        # Flag/provider/catalog outages are not proof that evidence is lost.
        raise PreferenceAdmissionUnavailable("runtime_job_recoverable")
    scope, digest = "user_learned_context", None
    # Retention may destroy raw evidence, but not an already-bound projection.
    # Changed/corrupt evidence is more ambiguous: do not narrow those outcomes.
    if code in {"source_expired", "source_redacted"}:
        try:
            foot = RuntimePreferenceFootprint.model_validate(job.runtime_withholding_footprint)
            if (job.runtime_route == RUNTIME_ROUTE and foot.job_id == job.id
                and foot.tenant_id == job.tenant_id and foot.proxy_user_id == job.proxy_user_id
                and foot.coverage == "targets"):
                scope, digest = "affected_context", foot.projection_sha256
        except (TypeError, ValueError):
            pass  # Preserve the full hold; never repair/synthesize a footprint.
    record = RuntimeReconciliationRecord(hold_scope=scope, footprint_sha256=digest,
        reason_code=code, operator_reason=reason, request_id=request_id)
    audit_id = uuid.uuid4()
    session.add(AuditLog(id=audit_id, action=AuditAction.updated, proxy_user_id=job.proxy_user_id,
        metadata_json={"event": "runtime_job_quarantined", "subject": "extraction_job",
                       "job_id": str(job.id), "tenant_id": str(job.tenant_id), **record.model_dump(mode="json")},
        ip_address=ip_address))
    # Flush the audit before the FK insert; both are still one transaction.
    session.flush()
    reconciliation = RuntimeJobReconciliation(job_id=job.id, tenant_id=job.tenant_id,
        proxy_user_id=job.proxy_user_id, audit_id=audit_id, record_json=record.model_dump(mode="json"))
    session.add(reconciliation)
    return reconciliation
