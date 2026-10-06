"""Sparse committed outcome proofs, not a second writer, classifier or scheduler."""
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import and_, or_, select, update

from api.db.models import (
    AuditAction,
    AuditLog,
    ExtractionJob,
    ExtractionJobStatus,
    Memory,
    MemoryClaim,
    MemoryClaimRevision,
    RuntimeJobReconciliation,
)
from api.schemas.runtime_reconciliation import (
    RuntimeReconciliationRecord,
    RuntimeResidualRecord,
)
from api.schemas.runtime_target_release import (
    RuntimeTargetReleases,
    TargetRelease,
    fact_key,
    footprint_keys,
    preference_key,
    read_target_releases,
)
from api.services.preference_choice_service import (
    CHOICE_OPERATION_MARKER,
    PreferenceChoiceOperation,
    lock_choice_target,
)
from api.services.preference_claim_service import (
    CHOICE_STATE_MARKER,
    PREFERENCE_MARKER,
    current_target,
    set_choice_state,
)
from api.services.preference_intent_admission_service import (
    PreferenceAdmissionUnavailable,
    _hash,
)
from api.services.preference_snapshot_service import snapshot_target
from api.services.runtime_preference_footprint import RuntimePreferenceFootprint
from api.services.runtime_preference_routing import (
    RUNTIME_ROUTE,
    target_release_enabled,
)
from api.services.source_review_service import target_version

MAX_RELEASE_JOBS = 8


@dataclass
class HeldTargets:
    footprint: RuntimePreferenceFootprint
    reconciliation: RuntimeJobReconciliation
    released: tuple[TargetRelease, ...]

    @property
    def remaining(self):
        return footprint_keys(self.footprint) - {t.key for t in self.released}


def prepare_target_releases(session, job, record):
    if not target_release_enabled(job.tenant_id) or record.runtime_source_sha256 is None:
        return ()
    # Quarantined work or an applied residual hold predating this source.
    # NOWAIT prevents holding a write transaction behind another recovery action.
    rows = session.execute(select(ExtractionJob.id, ExtractionJob.runtime_route,
        ExtractionJob.runtime_withholding_footprint, RuntimeJobReconciliation,
        ExtractionJob.runtime_applied_at, ExtractionJob.runtime_residual_hold).join(
        RuntimeJobReconciliation, RuntimeJobReconciliation.job_id == ExtractionJob.id).where(
        ExtractionJob.tenant_id == job.tenant_id, ExtractionJob.proxy_user_id == job.proxy_user_id,
        or_(and_(ExtractionJob.status == ExtractionJobStatus.dead, ExtractionJob.runtime_applied_at.is_(None)),
            and_(ExtractionJob.runtime_applied_at.is_not(None), ExtractionJob.runtime_residual_hold.is_not(None),
                 ExtractionJob.runtime_visibility_finalized_at.is_not(None))),
        ExtractionJob.runtime_hold_released_at.is_(None), ExtractionJob.created_at < job.created_at,
        RuntimeJobReconciliation.created_at < job.created_at,
    ).limit(MAX_RELEASE_JOBS).with_for_update(of=(ExtractionJob, RuntimeJobReconciliation), nowait=True)).all()
    # Release only proved targets of this page's jobs. Unseen jobs keep their
    # guards; this bounded write page never claims whole-user coverage.
    holds = []
    for old_id, route, raw, reconciliation, applied, residual in rows:
        try:
            if applied is not None:
                raw = residual
            foot = RuntimePreferenceFootprint.model_validate(raw)
            diagnosis = (RuntimeResidualRecord if applied is not None else RuntimeReconciliationRecord).model_validate(reconciliation.record_json)
            if (route != RUNTIME_ROUTE or foot.coverage != "targets" or foot.job_id != old_id
                or foot.tenant_id != job.tenant_id or foot.proxy_user_id != job.proxy_user_id
                or reconciliation.tenant_id != job.tenant_id or reconciliation.proxy_user_id != job.proxy_user_id
                or diagnosis.hold_scope != "affected_context" or diagnosis.footprint_sha256 != foot.projection_sha256):
                continue
            if applied is not None and (diagnosis.capture_id != foot.capture_id or diagnosis.decision_sha256 != foot.decision_sha256):
                continue
            releases = read_target_releases(reconciliation.release_json, foot)
        except (TypeError, ValueError):
            continue  # Corruption/unknown coverage never earns a release.
        holds.append(HeldTargets(foot, reconciliation, releases))
    return tuple(holds)


def can_reaffirm_runtime_target(holds, operation):
    return operation.action == "reaffirm" and any(preference_key(operation.fingerprint) in h.remaining for h in holds or ())


def commit_target_releases(session, job, record, holds, batch, stored, reaffirmations):
    if not holds:
        return 0
    # Reaffirm an already-current value only when the existing lock helper has
    # independently checked owned identity, exact version and <=20 authority.
    for operation in reaffirmations:
        backing_id = session.get(Memory, operation.target_memory_id).user_id
        ledger, target, reason = lock_choice_target(session, operation, user_id=
            backing_id)
        if reason != "preference_choice_not_held" or not can_reaffirm_runtime_target(holds, operation):
            raise PreferenceAdmissionUnavailable("runtime_reaffirm_target_changed")
        set_choice_state(target, ledger, unresolved=False, operation_key=operation.evidence_key)
    session.flush()
    stored_ids = {uuid.UUID(m.id) for m in stored}
    outcomes = []
    declarations = {m.validated_evidence["preference_fingerprint"]: m for m in batch.declarations}
    choices = {op.fingerprint: op for op in (*reaffirmations, *(PreferenceChoiceOperation.model_validate(
        m.validated_evidence[CHOICE_OPERATION_MARKER]) for m in batch.choices
        if m.validated_evidence[CHOICE_OPERATION_MARKER]["action"] == "reaffirm"))}
    fingerprints = declarations.keys() | choices.keys()
    if fingerprints:
        rows = session.execute(select(MemoryClaim, Memory, MemoryClaimRevision).join(
            Memory, Memory.id == MemoryClaim.active_memory_id).join(MemoryClaimRevision,
            MemoryClaimRevision.id == MemoryClaim.winning_revision_id).where(
            MemoryClaim.tenant_id == job.tenant_id, MemoryClaim.proxy_user_id == job.proxy_user_id,
            MemoryClaim.claim_fingerprint.in_(fingerprints), Memory.proxy_user_id == job.proxy_user_id,
            MemoryClaimRevision.claim_id == MemoryClaim.id, MemoryClaimRevision.memory_id == Memory.id,
        ).execution_options(populate_existing=True)).all()
        for claim, memory, revision in rows:
            fp = claim.claim_fingerprint
            offered = next(i for i in record.snapshot.identities if i.fingerprint == fp)
            scope = next(s for s in record.snapshot.catalog.scopes if s.ref == offered.scope_ref)
            try:
                current = snapshot_target(claim, memory, revision, property_key=offered.property_key, applicability=scope.applicability)
            except ValueError:
                continue
            if not current.is_current or current.choice_state != "current" or current.authority > 20:
                continue
            activated = (fp in declarations and memory.id in stored_ids
                and memory.source_conversation_id == record.source_conversation_id
                and revision.source_event_id == job.source_event_id
                and current.value == declarations[fp].validated_evidence[PREFERENCE_MARKER]["value"])
            reaffirmed = (fp in choices and memory.id == choices[fp].target_memory_id
                and (memory.metadata_json or {}).get(CHOICE_STATE_MARKER, {}).get("operation_key") == choices[fp].evidence_key)
            if activated or reaffirmed:
                outcomes.append({"kind": "preference", "key": preference_key(fp), "fingerprint": fp,
                    "fact_memory_id": None, "fact_version": None, "outcome": "activated" if activated else "reaffirmed",
                    "result_memory_id": memory.id, "result_version": current.memory_version,
                    "claim_id": claim.id, "revision_id": revision.id})
    if not {op.fingerprint for op in reaffirmations} <= {o["fingerprint"] for o in outcomes if o["outcome"] == "reaffirmed"}:
        raise PreferenceAdmissionUnavailable("runtime_reaffirm_outcome_missing")
    versions = {t.memory_id: t.version for t in record.fact_context}
    plans = {p.target_id: p for p in record.fact_plans or () if p.action == "UPDATE"}
    if plans and stored_ids:
        rows = session.scalars(select(Memory).where(Memory.id.in_(stored_ids), Memory.proxy_user_id == job.proxy_user_id)).all()
        for memory in rows:
            prior_id = memory.previous_version_id
            if (prior_id not in plans or not current_target(memory) or memory.category.value == "preference"
                or memory.source_conversation_id != record.source_conversation_id or memory.source_event_id != job.source_event_id):
                continue
            prior = session.get(Memory, prior_id)
            if prior is None or not prior.is_archived or prior.proxy_user_id != job.proxy_user_id:
                continue
            outcomes.append({"kind": "fact", "key": fact_key(prior_id, versions[prior_id]), "fingerprint": None,
                "fact_memory_id": prior_id, "fact_version": versions[prior_id], "outcome": "superseded",
                "result_memory_id": memory.id, "result_version": target_version(memory), "claim_id": None, "revision_id": None})
    for hold in holds:
        matches = [o for o in outcomes if o["key"] in hold.remaining]
        if not matches:
            continue
        audit_id = uuid.uuid4()
        added = tuple(TargetRelease(**o, source_job_id=job.id, source_capture_id=record.capture_id,
            source_sha256=record.runtime_source_sha256, decision_sha256=record.decision_sha256,
            audit_id=audit_id) for o in matches)
        foot = hold.footprint
        raw = {"schema_version": 1, "job_id": str(foot.job_id), "tenant_id": str(foot.tenant_id),
            "proxy_user_id": str(foot.proxy_user_id), "footprint_sha256": foot.projection_sha256,
            "targets": [t.model_dump(mode="json") for t in (*hold.released, *added)]}
        released = RuntimeTargetReleases.model_validate({**raw, "projection_sha256": _hash(raw)})
        complete = {t.key for t in released.targets} == footprint_keys(foot)
        session.add(AuditLog(id=audit_id, action=AuditAction.updated, proxy_user_id=job.proxy_user_id,
            metadata_json={"event": "runtime_targets_released", "job_id": str(foot.job_id),
                "source_job_id": str(job.id), "tenant_id": str(job.tenant_id), "actor_credential_class": "runtime_writer",
                "target_keys": [t.key for t in added], "complete": complete,
                "projection_sha256": released.projection_sha256}))
        hold.reconciliation.release_json = released.model_dump(mode="json")
        if complete:
            session.execute(update(ExtractionJob).where(ExtractionJob.id == foot.job_id,
                ExtractionJob.tenant_id == job.tenant_id, ExtractionJob.proxy_user_id == job.proxy_user_id,
                ExtractionJob.runtime_hold_released_at.is_(None)).values(runtime_hold_released_at=datetime.now(UTC)))
    # The audit flush and later receipt write must both succeed before commit.
    session.flush()
    return len(reaffirmations)
