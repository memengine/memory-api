from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import secrets
import time
import uuid
from dataclasses import asdict
from dataclasses import dataclass
from datetime import UTC
from datetime import datetime
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session
from sqlalchemy.orm import sessionmaker

from api.db.database import build_sync_session_factory
from api.db.models import TenantBudget
from api.utils.webhook_validator import validate_webhook_url


LOGGER = logging.getLogger("memoryos.webhook_events")
WEBHOOK_EVENT_TASK_NAME = "api.tasks.quota_tasks.send_webhook_event"


@dataclass(slots=True)
class WebhookEvent:
    event: str
    tenant_id: str
    timestamp: str
    data: dict[str, Any]
    memoryos_version: str = "1.0"


def generate_webhook_secret() -> str:
    return secrets.token_hex(32)


def utc_now_iso() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


class WebhookEventService:
    def __init__(
        self,
        *,
        session: AsyncSession | None = None,
        session_factory: sessionmaker[Session] | None = None,
        dispatch_task=None,
        client_factory=None,
    ) -> None:
        self.session = session
        self.session_factory = session_factory or build_sync_session_factory()
        self.dispatch_task = dispatch_task
        self.client_factory = client_factory or (lambda: httpx.Client(timeout=5.0))

    def send(self, tenant_id: str, event: str, data: dict[str, Any]) -> None:
        session = self.session_factory()
        try:
            budget = self._load_budget_sync(session, tenant_id)
            if budget is None or not budget.alert_webhook_url:
                return
            if not validate_webhook_url(budget.alert_webhook_url):
                LOGGER.warning(
                    "webhook_event_skipped_invalid_url tenant_id=%s event=%s",
                    tenant_id,
                    event,
                )
                return

            secret = self._ensure_webhook_secret(session, budget)
            timestamp = utc_now_iso()
            payload = WebhookEvent(
                event=event,
                tenant_id=tenant_id,
                timestamp=timestamp,
                data=data,
            )
            payload_bytes = json.dumps(asdict(payload), separators=(",", ":"), sort_keys=True).encode("utf-8")
            signature = hmac.new(secret.encode("utf-8"), payload_bytes, hashlib.sha256).hexdigest()
            headers = {
                "Content-Type": "application/json",
                "X-MemoryOS-Event": event,
                "X-MemoryOS-Timestamp": timestamp,
                "X-MemoryOS-Signature": signature,
            }

            with self.client_factory() as client:
                for attempt in range(3):
                    try:
                        response = client.post(
                            budget.alert_webhook_url,
                            content=payload_bytes,
                            headers=headers,
                        )
                        if 200 <= response.status_code < 300:
                            return
                    except httpx.HTTPError:
                        pass
                    if attempt < 2:
                        time.sleep(2**attempt)

            LOGGER.error(
                "webhook_event_delivery_failed tenant_id=%s event=%s",
                tenant_id,
                event,
            )
        except Exception:
            LOGGER.error(
                "webhook_event_send_unexpected_failure tenant_id=%s event=%s",
                tenant_id,
                event,
                exc_info=True,
            )
        finally:
            session.close()

    async def send_quota_warning(self, tenant_id: str, remaining_pct: float, threshold_pct: float) -> None:
        budget = await self._load_budget_async(tenant_id)
        data = {
            "remaining_pct": remaining_pct,
            "threshold_pct": threshold_pct,
            "reset_at": self._serialize_reset_at(budget.reset_at if budget else None),
            "upgrade_url": self._upgrade_url(),
        }
        await self._dispatch_event(tenant_id, "quota.warning", data)

    async def send_quota_critical(self, tenant_id: str, remaining_pct: float) -> None:
        budget = await self._load_budget_async(tenant_id)
        data = {
            "remaining_pct": remaining_pct,
            "threshold_pct": 0.05,
            "reset_at": self._serialize_reset_at(budget.reset_at if budget else None),
            "upgrade_url": self._upgrade_url(),
        }
        await self._dispatch_event(tenant_id, "quota.critical", data)

    async def send_quota_exhausted(self, tenant_id: str, mode: str) -> None:
        budget = await self._load_budget_async(tenant_id)
        data = {
            "mode": mode,
            "reset_at": self._serialize_reset_at(budget.reset_at if budget else None),
            "upgrade_url": self._upgrade_url(),
        }
        await self._dispatch_event(tenant_id, "quota.exhausted", data)

    async def send_quota_reset(self, tenant_id: str, new_limit: int | None, reset_at: str | None) -> None:
        await self._dispatch_event(
            tenant_id,
            "quota.reset",
            {
                "new_limit": new_limit,
                "reset_at": reset_at,
            },
        )

    async def send_mode_changed(self, tenant_id: str, from_mode: str, to_mode: str, reason: str) -> None:
        await self._dispatch_event(
            tenant_id,
            "mode.changed",
            {
                "from_mode": from_mode,
                "to_mode": to_mode,
                "reason": reason,
                "timestamp": utc_now_iso(),
            },
        )

    async def send_processing_delayed(self, tenant_id: str, queue: str, eta_seconds: int) -> None:
        await self._dispatch_event(
            tenant_id,
            "processing.delayed",
            {
                "queue": queue,
                "eta_seconds": eta_seconds,
            },
        )

    async def send_processing_recovered(self, tenant_id: str, queue: str) -> None:
        await self._dispatch_event(
            tenant_id,
            "processing.recovered",
            {
                "queue": queue,
            },
        )

    async def _dispatch_event(self, tenant_id: str, event: str, data: dict[str, Any]) -> None:
        if self.dispatch_task is None:
            return
        dispatched = self.dispatch_task(WEBHOOK_EVENT_TASK_NAME, [tenant_id, event, data])
        if dispatched is not None and hasattr(dispatched, "__await__"):
            await dispatched

    async def _load_budget_async(self, tenant_id: str) -> TenantBudget | None:
        if self.session is None:
            return None
        result = await self.session.execute(
            select(TenantBudget).where(TenantBudget.tenant_id == uuid.UUID(tenant_id))
        )
        return result.scalar_one_or_none()

    @staticmethod
    def _load_budget_sync(session: Session, tenant_id: str) -> TenantBudget | None:
        result = session.execute(
            select(TenantBudget).where(TenantBudget.tenant_id == uuid.UUID(tenant_id))
        )
        return result.scalar_one_or_none()

    @staticmethod
    def _serialize_reset_at(reset_at: datetime | None) -> str | None:
        if reset_at is None:
            return None
        return reset_at.replace(microsecond=0).isoformat().replace("+00:00", "Z")

    @staticmethod
    def _upgrade_url() -> str:
        return os.getenv("BILLING_UPGRADE_URL", "").strip()

    @staticmethod
    def _ensure_webhook_secret(session: Session, budget: TenantBudget) -> str:
        if budget.webhook_secret:
            return str(budget.webhook_secret)
        budget.webhook_secret = generate_webhook_secret()
        session.add(budget)
        session.commit()
        session.refresh(budget)
        return str(budget.webhook_secret)
