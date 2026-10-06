"""Bounded metadata-only repair; lost or invalid receipts remain withheld."""
import uuid

from sqlalchemy import select

from api.db.models import ExtractionJob, ProxyUser, Tenant
from api.services.preference_intent_admission_service import (
    PreferenceAdmissionUnavailable,
)
from api.services.runtime_fact_visibility import (
    project_fact_visibility,
    residual_footprint,
)
from api.services.runtime_preference_routing import RUNTIME_ROUTE
from api.services.runtime_preference_worker import RuntimePreferenceTransactionService


def backfill_visibility_batch(factory, *, tenant_id, after_id=None, limit=100, apply=False):
    """No provider, transcript reclassification, memory write or implicit tenant enrollment."""
    tenant_id = uuid.UUID(str(tenant_id))
    report = {"examined": 0, "eligible": 0, "projected": 0, "unrecoverable": 0, "next_id": None}
    with factory.begin() as session:
        tenant = session.get(Tenant, tenant_id)
        if tenant is None or not tenant.is_active:
            raise PreferenceAdmissionUnavailable("intent_tenant_unavailable")
        query = select(ExtractionJob).where(ExtractionJob.tenant_id == tenant_id,
            ExtractionJob.runtime_route == RUNTIME_ROUTE, ExtractionJob.runtime_applied_at.is_not(None),
            ExtractionJob.runtime_visibility_finalized_at.is_(None))
        if after_id:
            query = query.where(ExtractionJob.id > uuid.UUID(str(after_id)))
        if apply:
            query = query.with_for_update(nowait=True)
        jobs = session.scalars(query.order_by(ExtractionJob.id).limit(max(1, min(int(limit), 100)))).all()
        adapter = RuntimePreferenceTransactionService(factory)
        for job in jobs:
            report["examined"] += 1
            report["next_id"] = str(job.id)
            proxy = session.get(ProxyUser, job.proxy_user_id)
            if proxy is None or proxy.tenant_id != tenant_id or proxy.is_blocked:
                report["unrecoverable"] += 1
                continue
            try:
                record = adapter._read(job)
                if record is None or record.state != "applied":
                    raise PreferenceAdmissionUnavailable("runtime_visibility_receipt_missing")
                # Existing committed stamp plus a closed, bound immutable record;
                # this repair never reinterprets raw source or changes authority.
                residual_footprint(job, record)
                if record.tenant_id != job.tenant_id or record.proxy_user_id != job.proxy_user_id:
                    raise PreferenceAdmissionUnavailable("runtime_visibility_owner_changed")
            except (TypeError, ValueError):
                report["unrecoverable"] += 1
                continue
            report["eligible"] += 1
            if apply:
                project_fact_visibility(session, job, record)
                report["projected"] += 1
    return report
