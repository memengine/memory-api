from __future__ import annotations

import logging
import uuid
from datetime import date

from celery import shared_task
from celery.schedules import crontab
from sqlalchemy import select

from api.db.database import build_sync_session_factory
from api.db.models import EdTechMemory
from api.db.models import Tenant
from api.services.edtech.forgetting_curve import compute_forgetting_stage
from api.services.edtech.forgetting_curve import days_since


LOGGER = logging.getLogger(__name__)

EDTECH_FORGETTING_TASK_NAME = "api.tasks.edtech_tasks.update_forgetting_stages_for_all_edtech_tenants"
EDTECH_FORGETTING_BEAT_SCHEDULE = {
    "daily-edtech-forgetting": {
        "task": EDTECH_FORGETTING_TASK_NAME,
        "schedule": crontab(hour=4, minute=0),
    },
}


def _is_edtech_tenant(tenant: Tenant) -> bool:
    metadata = tenant.metadata_json or {}
    return bool(metadata.get("edtech_schema_enabled")) or metadata.get("domain_schema") == "edtech"


def _refresh_memory_forgetting(memory: EdTechMemory) -> bool:
    stages = dict(memory.forgetting_stages or {})
    changed = False
    today = date.today()
    for topic, record in list(stages.items()):
        if not isinstance(record, dict):
            continue
        elapsed = days_since(record.get("last_reviewed"), today=today)
        if elapsed is None:
            continue
        new_stage = compute_forgetting_stage(elapsed)
        if record.get("stage") != new_stage or record.get("days_since") != elapsed:
            record["stage"] = new_stage
            record["days_since"] = elapsed
            stages[topic] = record
            changed = True
    if changed:
        memory.forgetting_stages = stages
    return changed


def update_forgetting_stages_for_tenant(tenant_id: str) -> int:
    session_factory = build_sync_session_factory()
    session = session_factory()
    try:
        tenant_uuid = uuid.UUID(str(tenant_id))
        memories = (
            session.execute(select(EdTechMemory).where(EdTechMemory.tenant_id == tenant_uuid))
            .scalars()
            .all()
        )
        updated = 0
        for memory in memories:
            if _refresh_memory_forgetting(memory):
                session.add(memory)
                updated += 1
        session.commit()
        return updated
    finally:
        session.close()


@shared_task(name=EDTECH_FORGETTING_TASK_NAME)
def update_forgetting_stages_for_all_edtech_tenants() -> dict[str, int]:
    session_factory = build_sync_session_factory()
    session = session_factory()
    updated_by_tenant: dict[str, int] = {}
    try:
        tenants = session.execute(select(Tenant).where(Tenant.is_active.is_(True))).scalars().all()
        for tenant in tenants:
            if not _is_edtech_tenant(tenant):
                continue
            updated_by_tenant[str(tenant.id)] = update_forgetting_stages_for_tenant(str(tenant.id))
        LOGGER.info("edtech_forgetting_update_complete", extra={"event": "edtech_forgetting_update_complete", "tenants": len(updated_by_tenant)})
        return updated_by_tenant
    finally:
        session.close()


__all__ = [
    "EDTECH_FORGETTING_BEAT_SCHEDULE",
    "EDTECH_FORGETTING_TASK_NAME",
    "update_forgetting_stages_for_all_edtech_tenants",
    "update_forgetting_stages_for_tenant",
]
