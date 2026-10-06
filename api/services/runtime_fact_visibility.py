"""Operational receipt completion is independent of unresolved fact visibility."""
import uuid
from datetime import UTC, datetime

from sqlalchemy import or_

from api.db.models import AuditAction, AuditLog, ExtractionJob, RuntimeJobReconciliation
from api.schemas.runtime_reconciliation import RuntimeResidualRecord
from api.services.preference_intent_admission_service import (
    PreferenceAdmissionUnavailable,
    _hash,
)
from api.services.runtime_preference_footprint import (
    RuntimePreferenceFootprint,
    check_footprint,
)


def unresolved_runtime_predicate():
    return or_(ExtractionJob.runtime_applied_at.is_(None),
        ExtractionJob.runtime_visibility_finalized_at.is_(None), ExtractionJob.runtime_residual_hold.is_not(None))


def residual_footprint(job, record):
    original = check_footprint(job, record)
    if original is None:
        raise PreferenceAdmissionUnavailable("runtime_visibility_footprint_missing")
    receipt = record.receipt
    if receipt is None or record.state != "applied":
        raise PreferenceAdmissionUnavailable("runtime_visibility_receipt_missing")
    if not receipt.deferred_facts:
        return None
    # Legacy counts cannot identify which item deferred or was authority-vetoed.
    outcomes = receipt.fact_outcomes
    pending = [o for o in outcomes or () if o.state == "pending"]
    if outcomes is not None and not pending:
        return None  # A lower-authority challenge does not hide a stronger fact.
    unknown = outcomes is None or any(o.target is None for o in pending)
    targets = {o.target.memory_id: o.target for o in pending if o.target is not None}
    raw = original.model_dump(mode="json", exclude={"projection_sha256"})
    raw.update(coverage="user_learned_context" if unknown else "targets",
        reason=("incomplete_fact_context" if not record.fact_context_complete else "unknown_fact_target")
            if unknown else "known_targets", preference_fingerprints=[],
        fact_targets=[] if unknown else [targets[k].model_dump(mode="json") for k in sorted(targets)])
    return RuntimePreferenceFootprint.model_validate({**raw, "projection_sha256": _hash(raw)})


def project_fact_visibility(session, job, record):
    """Caller owns the job lock; projection/audit and source writes share a commit."""
    if record.tenant_id != job.tenant_id or record.proxy_user_id != job.proxy_user_id or job.runtime_applied_at is None:
        raise PreferenceAdmissionUnavailable("runtime_visibility_owner_or_receipt_changed")
    hold = residual_footprint(job, record)
    raw = hold.model_dump(mode="json") if hold is not None else None
    if job.runtime_visibility_finalized_at is not None:
        if job.runtime_residual_hold != raw:
            raise PreferenceAdmissionUnavailable("runtime_visibility_projection_changed")
        return  # Never reopen releases or reissue reviews on applied replay.
    if hold is not None:
        diagnosis = RuntimeResidualRecord(hold_scope="affected_context" if hold.coverage == "targets" else "user_learned_context",
            footprint_sha256=hold.projection_sha256, capture_id=record.capture_id, decision_sha256=record.decision_sha256)
        existing = session.get(RuntimeJobReconciliation, job.id)
        if existing is not None:
            raise PreferenceAdmissionUnavailable("runtime_visibility_diagnosis_already_fixed")
        audit_id = uuid.uuid4()
        session.add(AuditLog(id=audit_id, proxy_user_id=job.proxy_user_id, action=AuditAction.updated,
            metadata_json={"event": "runtime_fact_visibility_withheld", "job_id": str(job.id),
                "tenant_id": str(job.tenant_id), **diagnosis.model_dump(mode="json")}))
        session.flush()
        session.add(RuntimeJobReconciliation(job_id=job.id, tenant_id=job.tenant_id,
            proxy_user_id=job.proxy_user_id, record_json=diagnosis.model_dump(mode="json"), audit_id=audit_id))
    job.runtime_residual_hold = raw
    job.runtime_visibility_finalized_at = datetime.now(UTC)
