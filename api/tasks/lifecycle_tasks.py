from __future__ import annotations

import asyncio
import logging
import time
from datetime import UTC
from datetime import datetime

from celery import shared_task
from celery.schedules import crontab
from sqlalchemy import select

from api.db.cache import CacheService
from api.db.database import SessionLocal
from api.db.models import Tenant
from api.services.lifecycle_manager import MemoryLifecycleManager

from api.tasks.scoring_tasks import run_lifecycle_for_all_tenants


LOGGER = logging.getLogger(__name__)

WEEKLY_LIFECYCLE_TASK_NAME = "api.tasks.lifecycle_tasks.run_lifecycle_for_all_tenants_task"
DAILY_FORGETTING_TASK_NAME = "api.tasks.lifecycle_tasks.run_daily_forgetting_update"
TEMPORAL_VALIDITY_TASK_NAME = "api.tasks.lifecycle_tasks.run_temporal_validity_transitions"
TEMPORAL_VALIDITY_INTERVAL_MINUTES = 5

LIFECYCLE_TASK_BEAT_SCHEDULE = {
    "weekly-lifecycle": {
        "task": WEEKLY_LIFECYCLE_TASK_NAME,
        "schedule": crontab(day_of_week="sunday", hour=2, minute=0),
    },
    "daily-forgetting": {
        "task": DAILY_FORGETTING_TASK_NAME,
        "schedule": crontab(hour=4, minute=0),
    },
}

TEMPORAL_VALIDITY_BEAT_SCHEDULE = {
    "run-temporal-validity-transitions": {
        "task": TEMPORAL_VALIDITY_TASK_NAME,
        "schedule": crontab(minute=f"*/{TEMPORAL_VALIDITY_INTERVAL_MINUTES}"),
    }
}


async def run_temporal_validity_transitions_for_all_tenants(
    *, now: datetime | None = None
) -> dict[str, object]:
    """Run restart-safe catch-up while containing failures to one tenant."""
    started = time.perf_counter()
    reference_time = now or datetime.now(UTC)
    async with SessionLocal() as session:
        tenant_ids = list(
            (
                await session.execute(
                    select(Tenant.id)
                    .where(Tenant.is_active.is_(True))
                    .order_by(Tenant.created_at.asc())
                )
            ).scalars().all()
        )

    reports: list[dict[str, object]] = []
    failures: list[dict[str, str]] = []
    for tenant_id in tenant_ids:
        try:
            async with SessionLocal() as session:
                report = await MemoryLifecycleManager(
                    session=session,
                    cache_service=CacheService(),
                    now=reference_time,
                    enforce_off_peak=False,
                ).run_temporal_transitions_for_tenant(str(tenant_id))
                reports.append(report.to_dict())
        except Exception as exc:
            LOGGER.exception(
                "temporal_transition_tenant_failed",
                extra={
                    "event": "temporal_transition_tenant_failed",
                    "tenant_id": str(tenant_id),
                },
            )
            failures.append({"tenant_id": str(tenant_id), "error_type": type(exc).__name__})

    result: dict[str, object] = {
        "ran_at": reference_time.isoformat(),
        "schedule_interval_minutes": TEMPORAL_VALIDITY_INTERVAL_MINUTES,
        "tenant_count": len(tenant_ids),
        "successful_tenants": len(reports),
        "failed_tenants": len(failures),
        "activated_count": sum(int(row["activated_count"]) for row in reports),
        "expired_count": sum(int(row["expired_count"]) for row in reports),
        "duration_seconds": round(time.perf_counter() - started, 6),
        "tenant_reports": reports,
        "failures": failures,
    }
    LOGGER.info(
        "temporal_transition_cycle_report",
        extra={"event": "temporal_transition_cycle_report", **result},
    )
    return result


def is_peak_ist_window(now_utc: datetime | None = None) -> bool:
    """Return True for 09:00-22:00 IST using a UTC+05:30 offset."""
    reference = now_utc or datetime.now(UTC)
    if reference.tzinfo is None:
        reference = reference.replace(tzinfo=UTC)
    minutes_ist = (reference.hour * 60 + reference.minute + 330) % 1440
    return 9 * 60 <= minutes_ist < 22 * 60


@shared_task(name=WEEKLY_LIFECYCLE_TASK_NAME)
def run_lifecycle_for_all_tenants_task() -> list[dict[str, object]] | None:
    if is_peak_ist_window():
        LOGGER.info("lifecycle_skipped_peak_hours", extra={"event": "lifecycle_skipped_peak_hours"})
        return None
    return asyncio.run(run_lifecycle_for_all_tenants())


@shared_task(name=DAILY_FORGETTING_TASK_NAME)
def run_daily_forgetting_update() -> None:
    """Placeholder for domain-specific forgetting updates.

    EdTech schemas can implement the actual forgetting curve behavior later.
    Non-EdTech tenants safely no-op.
    """
    LOGGER.info("daily_forgetting_update_noop", extra={"event": "daily_forgetting_update_noop"})
    return None


@shared_task(name=TEMPORAL_VALIDITY_TASK_NAME)
def run_temporal_validity_transitions() -> dict[str, object]:
    return asyncio.run(run_temporal_validity_transitions_for_all_tenants())


__all__ = [
    "DAILY_FORGETTING_TASK_NAME",
    "LIFECYCLE_TASK_BEAT_SCHEDULE",
    "TEMPORAL_VALIDITY_BEAT_SCHEDULE",
    "TEMPORAL_VALIDITY_INTERVAL_MINUTES",
    "TEMPORAL_VALIDITY_TASK_NAME",
    "WEEKLY_LIFECYCLE_TASK_NAME",
    "is_peak_ist_window",
    "run_daily_forgetting_update",
    "run_lifecycle_for_all_tenants_task",
    "run_temporal_validity_transitions",
    "run_temporal_validity_transitions_for_all_tenants",
]
