from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime

from celery import shared_task
from celery.schedules import crontab
from sqlalchemy import select

from api.db.cache import CacheService
from api.db.database import scoped_async_session_factory
from api.db.models import Tenant
from api.db.vector_store import QdrantService
from api.services.lifecycle_manager import LifecycleReport, MemoryLifecycleManager

LOGGER = logging.getLogger(__name__)
LIFECYCLE_TASK_NAME = "api.tasks.scoring_tasks.run_weekly_memory_lifecycle"
LIFECYCLE_TASK_BEAT_SCHEDULE = {
    "run-weekly-memory-lifecycle": {
        "task": LIFECYCLE_TASK_NAME,
        "schedule": crontab(hour=2, minute=0, day_of_week="sun"),
    }
}


async def run_lifecycle_for_all_tenants(
    *,
    batch_size: int = 10,
    sleep_between_batches_seconds: float = 1.0,
    now: datetime | None = None,
    session_factory=None,
) -> list[dict[str, object]]:
    if session_factory is None:
        async with scoped_async_session_factory() as task_session_factory:
            return await run_lifecycle_for_all_tenants(
                batch_size=batch_size,
                sleep_between_batches_seconds=sleep_between_batches_seconds,
                now=now,
                session_factory=task_session_factory,
            )

    reference_time = now or datetime.now(UTC)
    reports: list[dict[str, object]] = []
    cache_service = CacheService()
    qdrant_service = QdrantService()

    async with session_factory() as session:
        tenant_ids = list(
            (
                await session.execute(
                    select(Tenant.id).where(Tenant.is_active.is_(True)).order_by(Tenant.created_at.asc())
                )
            ).scalars().all()
        )

    for index in range(0, len(tenant_ids), batch_size):
        batch = tenant_ids[index : index + batch_size]
        for tenant_id in batch:
            async with session_factory() as session:
                manager = MemoryLifecycleManager(
                    session=session,
                    cache_service=cache_service,
                    qdrant_service=qdrant_service,
                    now=reference_time,
                )
                report = await manager.run_for_tenant(str(tenant_id))
                reports.append(report.to_dict())
                LOGGER.info("tenant_lifecycle_report", extra={"event": "tenant_lifecycle_report", **report.to_dict()})
        if index + batch_size < len(tenant_ids):
            await asyncio.sleep(sleep_between_batches_seconds)

    return reports


@shared_task(name=LIFECYCLE_TASK_NAME)
def run_weekly_memory_lifecycle() -> list[dict[str, object]]:
    return asyncio.run(run_lifecycle_for_all_tenants())


__all__ = [
    "LIFECYCLE_TASK_BEAT_SCHEDULE",
    "LIFECYCLE_TASK_NAME",
    "LifecycleReport",
    "run_lifecycle_for_all_tenants",
    "run_weekly_memory_lifecycle",
]
