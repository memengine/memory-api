"""Bounded, provider-free holds. A projection never asserts currentness."""
from dataclasses import dataclass

from sqlalchemy import select

from api.db.models import ExtractionJob, Memory, RuntimeJobReconciliation
from api.schemas.runtime_reconciliation import (
    RuntimeReconciliationRecord,
    RuntimeResidualRecord,
)
from api.schemas.runtime_target_release import (
    fact_key,
    preference_key,
    read_target_releases,
)
from api.services.runtime_fact_visibility import unresolved_runtime_predicate
from api.services.runtime_preference_footprint import RuntimePreferenceFootprint
from api.services.runtime_preference_routing import (
    RUNTIME_ROUTE,
    target_release_enabled,
)
from api.services.source_review_service import target_version
from api.settings import get_settings

MAX_PENDING_FOOTPRINTS = 8


@dataclass(frozen=True)
class RuntimeHold:
    broad: bool = True
    unavailable: bool = False
    preference_fingerprints: frozenset[str] = frozenset()
    fact_ids: frozenset[str] = frozenset()


async def read_runtime_hold(session, *, owner, tenant_id, predicate, unavailable):
    settings = get_settings()
    if (not settings.runtime_target_withholding_enabled
        or str(tenant_id) not in settings.runtime_preference_tenant_ids.split(",")):
        return RuntimeHold(unavailable=unavailable)
    rows = (await session.execute(select(ExtractionJob.id, ExtractionJob.runtime_route,
        ExtractionJob.runtime_withholding_footprint, RuntimeJobReconciliation.record_json,
        RuntimeJobReconciliation.tenant_id, RuntimeJobReconciliation.proxy_user_id,
        RuntimeJobReconciliation.release_json, ExtractionJob.runtime_applied_at,
        ExtractionJob.runtime_visibility_finalized_at, ExtractionJob.runtime_residual_hold).outerjoin(
        RuntimeJobReconciliation, RuntimeJobReconciliation.job_id == ExtractionJob.id).where(
        ExtractionJob.tenant_id == tenant_id, ExtractionJob.proxy_user_id == owner,
        predicate, unresolved_runtime_predicate(),
        *([ExtractionJob.runtime_hold_released_at.is_(None)] if target_release_enabled(tenant_id) else []),
    ).limit(MAX_PENDING_FOOTPRINTS + 1))).all()
    # A page cannot prove coverage when more jobs exist. Unknown stays broad.
    if not rows or len(rows) > MAX_PENDING_FOOTPRINTS:
        return RuntimeHold(unavailable=unavailable)
    prefs, facts = set(), {}
    for job_id, route, raw, reconciled, reconciled_tenant, reconciled_owner, release_json, applied, finalized, residual in rows:
        if applied is not None:
            if finalized is None or residual is None or reconciled is None:
                return RuntimeHold(unavailable=True)  # Historical completion is not resolution proof.
            raw = residual
        if route != RUNTIME_ROUTE or raw is None:
            return RuntimeHold(unavailable=unavailable)
        try:
            footprint = RuntimePreferenceFootprint.model_validate(raw)
            if (footprint.job_id != job_id or footprint.tenant_id != tenant_id
                or footprint.proxy_user_id != owner):
                raise ValueError("foreign footprint")
        except (TypeError, ValueError):
            return RuntimeHold(unavailable=True)
        if reconciled is not None:
            try:
                record = (RuntimeResidualRecord if applied is not None else RuntimeReconciliationRecord).model_validate(reconciled)
                if (reconciled_tenant != tenant_id or reconciled_owner != owner
                    or record.hold_scope != "affected_context"
                    or record.footprint_sha256 != footprint.projection_sha256):
                    return RuntimeHold(unavailable=True)
                if applied is not None and (record.capture_id != footprint.capture_id
                    or record.decision_sha256 != footprint.decision_sha256):
                    return RuntimeHold(unavailable=True)
            except (TypeError, ValueError):
                return RuntimeHold(unavailable=True)
        if footprint.coverage != "targets":
            return RuntimeHold(unavailable=unavailable)
        try:
            released = ({t.key for t in read_target_releases(release_json, footprint)}
                if target_release_enabled(tenant_id) and reconciled is not None else set())
        except (TypeError, ValueError):
            return RuntimeHold(unavailable=True)
        # A complete release and its indexed timestamp must commit together.
        if release_json is not None and released and len(released) == len(footprint.preference_fingerprints) + len(footprint.fact_targets):
            return RuntimeHold(unavailable=True)
        prefs.update(fp for fp in footprint.preference_fingerprints if preference_key(fp) not in released)
        for target in footprint.fact_targets:
            if fact_key(target.memory_id, target.version) in released:
                continue
            if target.memory_id in facts and facts[target.memory_id] != target.version:
                return RuntimeHold(unavailable=True)
            facts[target.memory_id] = target.version
    if facts:
        memories = list((await session.scalars(select(Memory).where(
            Memory.id.in_(facts), Memory.proxy_user_id == owner,
        ).execution_options(populate_existing=True))).all())
        if (len(memories) != len(facts)
            or any(target_version(m) != facts[m.id] or m.is_archived for m in memories)):
            # Never follow a changed/missing target into a guessed successor.
            return RuntimeHold(unavailable=True)
    return RuntimeHold(broad=False, unavailable=unavailable,
                       preference_fingerprints=frozenset(prefs), fact_ids=frozenset(map(str, facts)))
