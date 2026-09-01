from __future__ import annotations

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from api.celery_app import celery_app
from api.tasks import lifecycle_tasks


class _Scalars:
    def __init__(self, values):
        self.values = values

    def scalars(self):
        return self

    def all(self):
        return list(self.values)


class _Session:
    def __init__(self, tenant_ids=None):
        self.tenant_ids = tenant_ids or []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    async def execute(self, _statement):
        return _Scalars(self.tenant_ids)


def test_temporal_transition_schedule_is_registered_at_five_minutes() -> None:
    schedule = celery_app.conf.beat_schedule["run-temporal-validity-transitions"]
    assert schedule["task"] == lifecycle_tasks.TEMPORAL_VALIDITY_TASK_NAME
    assert lifecycle_tasks.TEMPORAL_VALIDITY_INTERVAL_MINUTES == 5
    assert str(schedule["schedule"]) == "<crontab: */5 * * * * (m/h/dM/MY/d)>"


@pytest.mark.asyncio
async def test_transition_cycle_reports_counts_latency_and_tenant_failures(monkeypatch) -> None:
    tenant_ids = [uuid.uuid4(), uuid.uuid4()]
    sessions = iter([_Session(tenant_ids), _Session(), _Session()])
    monkeypatch.setattr(lifecycle_tasks, "SessionLocal", lambda: next(sessions))
    monkeypatch.setattr(lifecycle_tasks, "CacheService", lambda: object())

    class _Manager:
        def __init__(self, **_kwargs):
            pass

        async def run_temporal_transitions_for_tenant(self, tenant_id):
            if tenant_id == str(tenant_ids[1]):
                raise RuntimeError("contained tenant failure")
            return SimpleNamespace(
                to_dict=lambda: {
                    "tenant_id": tenant_id,
                    "activated_count": 2,
                    "expired_count": 3,
                    "duration_seconds": 0.01,
                }
            )

    monkeypatch.setattr(lifecycle_tasks, "MemoryLifecycleManager", _Manager)
    result = await lifecycle_tasks.run_temporal_validity_transitions_for_all_tenants(
        now=datetime(2026, 8, 13, 12, 0, tzinfo=UTC)
    )

    assert result["tenant_count"] == 2
    assert result["successful_tenants"] == 1
    assert result["failed_tenants"] == 1
    assert result["activated_count"] == 2
    assert result["expired_count"] == 3
    assert result["duration_seconds"] >= 0
    assert result["failures"] == [
        {"tenant_id": str(tenant_ids[1]), "error_type": "RuntimeError"}
    ]
