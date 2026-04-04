from __future__ import annotations

from unittest.mock import AsyncMock
from unittest.mock import MagicMock

import pytest

from api.db.models import QuotaMode
from api.services.memory_service import MemoryService
from api.services.quota_manager import QuotaEnvelope


class FakeQuotaManager:
    def __init__(self, envelope: QuotaEnvelope) -> None:
        self.envelope = envelope
        self.get_quota_envelope = AsyncMock(return_value=envelope)


class FakeDispatchTask:
    def __init__(self) -> None:
        self.calls = []

    def __call__(self, task_name, *args, **kwargs):
        self.calls.append((task_name, args, kwargs))
        return None


@pytest.mark.asyncio
async def test_queue_memory_add_returns_blocked_when_quota_mode_is_blocked() -> None:
    quota_manager = FakeQuotaManager(
        QuotaEnvelope(mode=QuotaMode.blocked, budget_remaining_pct=0.0, reset_at=None)
    )
    cache_service = MagicMock()
    cache_service.get_idempotent_response = AsyncMock(return_value=None)
    cache_service.set_job_status = AsyncMock()
    cache_service.set_idempotent_response = AsyncMock()

    service = MemoryService(
        session=MagicMock(),
        cache_service=cache_service,
        qdrant_service=MagicMock(),
        quota_manager=quota_manager,
    )

    result = await service.queue_memory_add(
        requested_user_id="user_123",
        authenticated_user_id=None,
        agent_id=None,
        messages=[{"role": "user", "content": "hello"}],
        metadata={},
        idempotency_key=None,
        tenant_id="tenant_123",
        external_user_id="external_123",
    )

    assert result["status"] == "blocked"
    assert result["job_id"] is None
    assert result["budget_remaining_pct"] == 0.0
    cache_service.set_job_status.assert_not_awaited()


@pytest.mark.asyncio
async def test_queue_memory_add_returns_passthrough_when_quota_mode_is_passthrough() -> None:
    quota_manager = FakeQuotaManager(
        QuotaEnvelope(mode=QuotaMode.passthrough, budget_remaining_pct=0.22, reset_at=None)
    )
    cache_service = MagicMock()
    cache_service.get_idempotent_response = AsyncMock(return_value=None)
    cache_service.set_job_status = AsyncMock()
    cache_service.set_idempotent_response = AsyncMock()

    service = MemoryService(
        session=MagicMock(),
        cache_service=cache_service,
        qdrant_service=MagicMock(),
        quota_manager=quota_manager,
    )

    result = await service.queue_memory_add(
        requested_user_id="user_123",
        authenticated_user_id=None,
        agent_id=None,
        messages=[{"role": "user", "content": "hello"}],
        metadata={},
        idempotency_key=None,
        tenant_id="tenant_123",
        external_user_id="external_123",
    )

    assert result["status"] == "passthrough"
    assert result["job_id"] is None
    assert result["budget_remaining_pct"] == 0.22
    cache_service.set_job_status.assert_not_awaited()


@pytest.mark.asyncio
async def test_queue_memory_add_dispatches_extraction_task_when_queued() -> None:
    quota_manager = FakeQuotaManager(
        QuotaEnvelope(mode=QuotaMode.full, budget_remaining_pct=0.88, reset_at=None)
    )
    cache_service = MagicMock()
    cache_service.get_idempotent_response = AsyncMock(return_value=None)
    cache_service.set_job_status = AsyncMock()
    cache_service.set_idempotent_response = AsyncMock()
    dispatch_task = FakeDispatchTask()

    service = MemoryService(
        session=MagicMock(),
        cache_service=cache_service,
        qdrant_service=MagicMock(),
        quota_manager=quota_manager,
        dispatch_task=dispatch_task,
    )

    result = await service.queue_memory_add(
        requested_user_id=None,
        authenticated_user_id=None,
        agent_id=None,
        messages=[{"role": "user", "content": "hello"}],
        metadata={"session_id": "sess_1"},
        idempotency_key=None,
        tenant_id=None,
        external_user_id=None,
    )

    assert result["status"] == "queued"
    assert result["job_id"] is not None
    cache_service.set_job_status.assert_awaited_once()
    assert dispatch_task.calls[0][0] == "api.tasks.extraction_tasks.process_extraction_job"
    assert dispatch_task.calls[0][2]["args"][0]["job_id"] == result["job_id"]
    assert dispatch_task.calls[0][2]["queue"] is None
