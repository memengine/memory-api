from __future__ import annotations

import uuid
from unittest.mock import AsyncMock
from unittest.mock import MagicMock

import fakeredis.aioredis
import pytest

from api.tasks.queue_router import ENTERPRISE_QUEUE
from api.tasks.queue_router import PLAN_CACHE_TTL_SECONDS
from api.tasks.queue_router import QueueRouter
from api.tasks.queue_router import STARTER_QUEUE


class FakeScalarResult:
    def __init__(self, value) -> None:
        self.value = value

    def scalar_one_or_none(self):
        return self.value


class FakeCacheService:
    def __init__(self) -> None:
        self.client = fakeredis.aioredis.FakeRedis(decode_responses=True)


@pytest.mark.asyncio
async def test_get_extraction_queue_uses_cached_plan() -> None:
    tenant_id = str(uuid.uuid4())
    cache_service = FakeCacheService()
    await cache_service.client.set(f"tenant:{tenant_id}:plan", "enterprise", ex=PLAN_CACHE_TTL_SECONDS)
    session = MagicMock()
    session.execute = AsyncMock()
    session.get = AsyncMock()
    router = QueueRouter(session=session, cache_service=cache_service)

    queue_name = await router.get_extraction_queue(tenant_id)

    assert queue_name == ENTERPRISE_QUEUE
    session.execute.assert_not_awaited()
    session.get.assert_not_awaited()


@pytest.mark.asyncio
async def test_get_extraction_queue_defaults_to_starter_when_plan_unknown() -> None:
    tenant_id = str(uuid.uuid4())
    cache_service = FakeCacheService()
    session = MagicMock()
    session.execute = AsyncMock(return_value=FakeScalarResult(None))
    session.get = AsyncMock(return_value=None)
    router = QueueRouter(session=session, cache_service=cache_service)

    queue_name = await router.get_extraction_queue(tenant_id)

    assert queue_name == STARTER_QUEUE
    assert await cache_service.client.get(f"tenant:{tenant_id}:plan") == "starter"


@pytest.mark.asyncio
async def test_reserve_extraction_slot_returns_none_when_plan_limit_reached() -> None:
    tenant_id = str(uuid.uuid4())
    cache_service = FakeCacheService()
    session = MagicMock()
    router = QueueRouter(session=session, cache_service=cache_service)
    await cache_service.client.set(f"tenant:{tenant_id}:plan", "starter", ex=PLAN_CACHE_TTL_SECONDS)
    await cache_service.client.set(f"tenant_queue_depth:{tenant_id}:starter-extraction", "50", ex=600)

    reservation = await router.reserve_extraction_slot(tenant_id=tenant_id, job_id="job-limit")

    assert reservation is None


@pytest.mark.asyncio
async def test_reserve_slot_and_inspect_queue_snapshot() -> None:
    tenant_id = str(uuid.uuid4())
    cache_service = FakeCacheService()
    session = MagicMock()
    router = QueueRouter(session=session, cache_service=cache_service)
    await cache_service.client.set(f"tenant:{tenant_id}:plan", "enterprise", ex=PLAN_CACHE_TTL_SECONDS)

    reservation = await router.reserve_extraction_slot(tenant_id=tenant_id, job_id="job-123")
    snapshot = await router.inspect_all_queues()

    assert reservation is not None
    assert reservation.queue_name == ENTERPRISE_QUEUE
    assert snapshot[ENTERPRISE_QUEUE]["tenant_breakdown"][tenant_id] == 1
    assert snapshot[ENTERPRISE_QUEUE]["oldest_job_age_seconds"] is not None
    assert snapshot[ENTERPRISE_QUEUE]["oldest_job_age_seconds"] >= 0

    await router.release_extraction_slot(
        tenant_id=tenant_id,
        queue_name=ENTERPRISE_QUEUE,
        job_id="job-123",
    )
    snapshot_after_release = await router.inspect_all_queues()
    assert snapshot_after_release[ENTERPRISE_QUEUE]["tenant_breakdown"] == {}
