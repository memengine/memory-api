from __future__ import annotations

from datetime import UTC
from datetime import datetime
from typing import Any

import httpx
from celery import shared_task
from sqlalchemy import text
from sqlalchemy.orm import Session

from api.db.database import build_sync_session_factory


DEPRECATION_ALERT_TASK_NAME = "api.tasks.deprecation_tasks.send_deprecation_alerts"
DEPRECATION_ALERT_BEAT_SCHEDULE = {
    "send_deprecation_alerts": {
        "task": DEPRECATION_ALERT_TASK_NAME,
        "schedule": 86400.0,
    }
}
ALERT_WINDOWS_DAYS = {30, 7, 1}


def _build_session() -> Session:
    return build_sync_session_factory()()


def run_deprecation_alerts(*, now: datetime | None = None) -> dict[str, Any]:
    reference_now = now or datetime.now(UTC)
    sent = 0
    checked = 0
    session = _build_session()
    try:
        rows = session.execute(
            text(
                """
                SELECT
                    tdu.tenant_id,
                    tdu.field_path,
                    tdu.last_used_at,
                    adf.sunset_at,
                    adf.migration_guide_url,
                    tb.alert_webhook_url
                FROM tenant_deprecation_usage tdu
                JOIN api_deprecated_fields adf
                  ON adf.api_version = tdu.api_version
                 AND adf.field_path = tdu.field_path
                JOIN tenant_budgets tb
                  ON tb.tenant_id = tdu.tenant_id
                WHERE tb.alert_webhook_url IS NOT NULL
                """
            )
        ).mappings().all()

        for row in rows:
            checked += 1
            sunset_at = row["sunset_at"]
            if sunset_at is None:
                continue
            days_until_sunset = (sunset_at.date() - reference_now.date()).days
            if days_until_sunset not in ALERT_WINDOWS_DAYS:
                continue

            payload = {
                "event": "deprecated_field_sunset_warning",
                "tenant_id": str(row["tenant_id"]),
                "field_path": str(row["field_path"]),
                "last_used_at": row["last_used_at"].isoformat() if row["last_used_at"] else None,
                "sunset_at": sunset_at.isoformat(),
                "days_until_sunset": days_until_sunset,
                "migration_guide_url": str(row["migration_guide_url"]),
            }
            try:
                response = httpx.post(str(row["alert_webhook_url"]), json=payload, timeout=5.0)
                if response.is_success:
                    sent += 1
            except httpx.HTTPError:
                continue
        return {"checked": checked, "sent": sent, "timestamp": reference_now.isoformat()}
    finally:
        session.close()


@shared_task(name=DEPRECATION_ALERT_TASK_NAME)
def send_deprecation_alerts() -> dict[str, Any]:
    return run_deprecation_alerts()
