from __future__ import annotations

import calendar
from datetime import UTC
from datetime import datetime
from datetime import timedelta

from celery import shared_task
from celery.schedules import crontab
from sqlalchemy import select
from sqlalchemy import delete
from sqlalchemy.orm import sessionmaker
from sqlalchemy import create_engine

from api.db.database import get_sync_database_url
from api.db.models import QuotaMode
from api.db.models import SharedContextSignal
from api.db.models import TenantBudget
from api.services.webhook_event_service import WebhookEventService
from api.services.webhook_event_service import WEBHOOK_EVENT_TASK_NAME


MONTHLY_QUOTA_RESET_TASK_NAME = "api.tasks.quota_tasks.monthly_quota_reset_task"
SHARED_CONTEXT_SIGNAL_CLEANUP_TASK_NAME = "api.tasks.quota_tasks.cleanup_superseded_shared_context_signals"
QUOTA_TASK_BEAT_SCHEDULE = {
    "monthly-quota-reset": {
        "task": MONTHLY_QUOTA_RESET_TASK_NAME,
        "schedule": crontab(minute=0),
    },
    "cleanup-superseded-shared-context-signals": {
        "task": SHARED_CONTEXT_SIGNAL_CLEANUP_TASK_NAME,
        "schedule": crontab(hour=2, minute=30),
    }
}


@shared_task(name=WEBHOOK_EVENT_TASK_NAME)
def send_webhook_event(tenant_id: str, event: str, data: dict) -> bool:
    engine = create_engine(get_sync_database_url(), pool_pre_ping=True)
    session_factory = sessionmaker(bind=engine, expire_on_commit=False)
    service = WebhookEventService(session_factory=session_factory)
    service.send(tenant_id, event, data)
    return True


@shared_task(name=MONTHLY_QUOTA_RESET_TASK_NAME)
def monthly_quota_reset_task() -> dict[str, int]:
    engine = create_engine(get_sync_database_url(), pool_pre_ping=True)
    session_factory = sessionmaker(bind=engine, expire_on_commit=False)
    now = datetime.now(UTC)
    checked = 0
    reset_count = 0

    with session_factory() as session:
        budgets = session.execute(
            select(TenantBudget).where(
                TenantBudget.reset_at.is_not(None),
                TenantBudget.reset_at <= now,
            )
        ).scalars().all()

        for budget in budgets:
            checked += 1
            next_reset_at = _next_month_reset_at(budget.reset_at or now)
            budget.current_month_calls = 0
            budget.current_month_tokens = 0
            budget.write_calls = 0
            budget.read_calls = 0
            budget.last_notified_mode = QuotaMode.full
            budget.last_notified_pct = None
            budget.reset_at = next_reset_at
            session.add(budget)
            session.commit()
            send_webhook_event.delay(
                str(budget.tenant_id),
                "quota.reset",
                {
                    "new_limit": budget.monthly_call_limit,
                    "reset_at": next_reset_at.replace(microsecond=0).isoformat().replace("+00:00", "Z"),
                },
            )
            reset_count += 1

    return {"checked": checked, "reset": reset_count}


@shared_task(name=SHARED_CONTEXT_SIGNAL_CLEANUP_TASK_NAME)
def cleanup_superseded_shared_context_signals() -> dict[str, int]:
    engine = create_engine(get_sync_database_url(), pool_pre_ping=True)
    session_factory = sessionmaker(bind=engine, expire_on_commit=False)
    cutoff = datetime.now(UTC) - timedelta(days=90)

    with session_factory() as session:
        result = session.execute(
            delete(SharedContextSignal).where(
                SharedContextSignal.is_superseded.is_(True),
                SharedContextSignal.created_at < cutoff,
            )
        )
        deleted = int(result.rowcount or 0)
        session.commit()
    return {"deleted": deleted}


def _next_month_reset_at(value: datetime) -> datetime:
    normalized = value.astimezone(UTC)
    if normalized.month == 12:
        year = normalized.year + 1
        month = 1
    else:
        year = normalized.year
        month = normalized.month + 1
    day = min(normalized.day, calendar.monthrange(year, month)[1])
    return normalized.replace(year=year, month=month, day=day)
