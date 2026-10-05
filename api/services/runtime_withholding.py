"""Bounded, provider-free holds. A projection never asserts currentness."""
from dataclasses import dataclass

from sqlalchemy import select

from api.db.models import ExtractionJob, Memory, RuntimeJobReconciliation
from api.schemas.runtime_reconciliation import RuntimeReconciliationRecord
from api.services.runtime_preference_footprint import RuntimePreferenceFootprint
from api.services.runtime_preference_routing import RUNTIME_ROUTE
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
        RuntimeJobReconciliation.tenant_id, RuntimeJobReconciliation.proxy_user_id).outerjoin(
        RuntimeJobReconciliation, RuntimeJobReconciliation.job_id == ExtractionJob.id).where(
        ExtractionJob.tenant_id == tenant_id, ExtractionJob.proxy_user_id == owner,
        predicate, ExtractionJob.runtime_applied_at.is_(None),
    ).limit(MAX_PENDING_FOOTPRINTS + 1))).all()
    # A page cannot prove coverage when more jobs exist. Unknown stays broad.
    if not rows or len(rows) > MAX_PENDING_FOOTPRINTS:
        return RuntimeHold(unavailable=unavailable)
    prefs, facts = set(), {}
    for job_id, route, raw, reconciled, reconciled_tenant, reconciled_owner in rows:
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
                record = RuntimeReconciliationRecord.model_validate(reconciled)
                if (reconciled_tenant != tenant_id or reconciled_owner != owner
                    or record.hold_scope != "affected_context"
                    or record.footprint_sha256 != footprint.projection_sha256):
                    return RuntimeHold(unavailable=True)
            except (TypeError, ValueError):
                return RuntimeHold(unavailable=True)
        if footprint.coverage != "targets":
            return RuntimeHold(unavailable=unavailable)
        prefs.update(footprint.preference_fingerprints)
        for target in footprint.fact_targets:
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
