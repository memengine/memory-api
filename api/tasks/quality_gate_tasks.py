from __future__ import annotations

import os
import uuid

from celery import shared_task
from celery.signals import worker_process_shutdown
from sqlalchemy import create_engine
from sqlalchemy import select
from sqlalchemy.orm import Session
from sqlalchemy.orm import sessionmaker

from api.db.database import get_sync_database_url
from api.db.models import TenantBudget
from api.infra.postgres_benchmark import instrument_engine
from api.infra.postgres_benchmark import session_factory_owner
from api.services.webhook_event_service import WebhookEventService


_QUALITY_GATE_SESSION_FACTORY: sessionmaker[Session] | None = None
_QUALITY_GATE_SESSION_FACTORY_PID: int | None = None


def build_quality_gate_session_factory() -> sessionmaker[Session]:
    global _QUALITY_GATE_SESSION_FACTORY
    global _QUALITY_GATE_SESSION_FACTORY_PID

    process_id = os.getpid()
    if (
        _QUALITY_GATE_SESSION_FACTORY is None
        or _QUALITY_GATE_SESSION_FACTORY_PID != process_id
    ):
        dispose_quality_gate_session_factory()
        engine = create_engine(get_sync_database_url(), pool_pre_ping=True)
        instrument_engine(engine, kind="sync", owner=session_factory_owner())
        _QUALITY_GATE_SESSION_FACTORY = sessionmaker(bind=engine, expire_on_commit=False)
        _QUALITY_GATE_SESSION_FACTORY_PID = process_id
    return _QUALITY_GATE_SESSION_FACTORY


@worker_process_shutdown.connect
def dispose_quality_gate_session_factory(**_extra: object) -> None:
    global _QUALITY_GATE_SESSION_FACTORY
    global _QUALITY_GATE_SESSION_FACTORY_PID

    factory = _QUALITY_GATE_SESSION_FACTORY
    _QUALITY_GATE_SESSION_FACTORY = None
    _QUALITY_GATE_SESSION_FACTORY_PID = None
    if factory is None:
        return
    engine = factory.kw.get("bind")
    if engine is not None:
        engine.dispose()


@shared_task(name="api.tasks.quality_gate_tasks.increment_tenant_budget_usage")
def increment_tenant_budget_usage(tenant_id: str, estimated_tokens: int) -> bool:
    session_factory = build_quality_gate_session_factory()

    with session_factory() as session:
        tenant_budget = _get_tenant_budget(session, tenant_id)
        if tenant_budget is None:
            return False

        tenant_budget.current_month_calls = int(tenant_budget.current_month_calls or 0) + 1
        tenant_budget.current_month_tokens = int(tenant_budget.current_month_tokens or 0) + int(estimated_tokens)
        session.commit()
    return True


@shared_task(
    bind=True,
    name="api.tasks.quality_gate_tasks.send_budget_alert",
    max_retries=3,
    default_retry_delay=2,
)
def send_budget_alert(self, tenant_id: str, usage_pct: float, estimated_tokens: int) -> bool:
    session_factory = build_quality_gate_session_factory()

    with session_factory() as session:
        tenant_budget = _get_tenant_budget(session, tenant_id)
        if tenant_budget is None or not tenant_budget.alert_webhook_url:
            return False

        threshold_pct = float(tenant_budget.alert_threshold_pct or 0.8)
        remaining_pct = max(0.0, round(1.0 - float(usage_pct), 4))
        if remaining_pct > threshold_pct:
            return False
        if tenant_budget.last_notified_pct is not None and float(tenant_budget.last_notified_pct) <= threshold_pct:
            return False

        tenant_budget.last_notified_pct = threshold_pct
        session.add(tenant_budget)
        session.commit()

        service = WebhookEventService(session_factory=session_factory)
        service.send(
            tenant_id,
            "quota.warning",
            {
                "remaining_pct": remaining_pct,
                "threshold_pct": threshold_pct,
                "reset_at": tenant_budget.reset_at.replace(microsecond=0).isoformat().replace("+00:00", "Z")
                if tenant_budget.reset_at
                else None,
                "upgrade_url": os.getenv("BILLING_UPGRADE_URL", "").strip(),
            },
        )
        return True


def _get_tenant_budget(session: Session, tenant_id: str) -> TenantBudget | None:
    result = session.execute(
        select(TenantBudget).where(TenantBudget.tenant_id == uuid.UUID(tenant_id))
    )
    return result.scalar_one_or_none()
