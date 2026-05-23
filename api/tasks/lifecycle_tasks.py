from __future__ import annotations

import asyncio
import logging
from datetime import UTC
from datetime import datetime

from celery import shared_task
from celery.schedules import crontab

from api.tasks.scoring_tasks import run_lifecycle_for_all_tenants


LOGGER = logging.getLogger(__name__)

WEEKLY_LIFECYCLE_TASK_NAME = "api.tasks.lifecycle_tasks.run_lifecycle_for_all_tenants_task"
DAILY_FORGETTING_TASK_NAME = "api.tasks.lifecycle_tasks.run_daily_forgetting_update"

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


__all__ = [
    "DAILY_FORGETTING_TASK_NAME",
    "LIFECYCLE_TASK_BEAT_SCHEDULE",
    "WEEKLY_LIFECYCLE_TASK_NAME",
    "is_peak_ist_window",
    "run_daily_forgetting_update",
    "run_lifecycle_for_all_tenants_task",
]
