from __future__ import annotations

import asyncio
import uuid
from unittest.mock import AsyncMock
from unittest.mock import MagicMock

import fakeredis.aioredis
import pytest

from api.tasks.queue_router import ENTERPRISE_QUEUE
from api.tasks.queue_router import PLAN_CACHE_TTL_SECONDS
from api.tasks.queue_router import QueueRouter
from api.tasks.queue_router import RELEASE_EXTRACTION_SLOT_SCRIPT
from api.tasks.queue_router import RESERVE_EXTRACTION_SLOT_SCRIPT
from api.tasks.queue_router import STARTER_QUEUE


class ScriptCapableFakeRedis:
    """Minimal Lua adapter; real script behavior is covered by Redis integration tests."""

    def __init__(self) -> None:
        self._client = fakeredis.aioredis.FakeRedis(decode_responses=True)
        self._script_lock = asyncio.Lock()

    def __getattr__(self, name):
        return getattr(self._client, name)

    async def eval(self, script, _key_count, depth_key, jobs_key, breakdown_key, *args):
        async with self._script_lock:
            if script == RESERVE_EXTRACTION_SLOT_SCRIPT:
                metrics_key, *args = args
                member, tenant_id, queue_limit, ttl, now_score = args
                if await self._client.zscore(jobs_key, member) is not None:
                    return 2
                await self._client.hincrby(metrics_key, "attempted", 1)
                await self._client.expire(metrics_key, 172800)
                if int(await self._client.get(depth_key) or 0) >= int(queue_limit):
                    await self._client.hincrby(metrics_key, "full", 1)
                    return 0
                await self._client.incr(depth_key)
                await self._client.expire(depth_key, int(ttl))
                await self._client.zadd(jobs_key, {member: float(now_score)})
                await self._client.expire(jobs_key, int(ttl))
                await self._client.hincrby(breakdown_key, tenant_id, 1)
                await self._client.expire(breakdown_key, int(ttl))
                return 1
            if script == RELEASE_EXTRACTION_SLOT_SCRIPT:
                member, tenant_id = args
                if not await self._client.zrem(jobs_key, member):
                    return 0
                depth = await self._client.decr(depth_key)
                if int(depth) <= 0:
                    await self._client.delete(depth_key)
                tenant_depth = await self._client.hincrby(breakdown_key, tenant_id, -1)
                if int(tenant_depth) <= 0:
                    await self._client.hdel(breakdown_key, tenant_id)
                return 1
            raise AssertionError("Unexpected Redis script")


class FakeScalarResult:
    def __init__(self, value) -> None:
        self.value = value

    def scalar_one_or_none(self):
        return self.value


class FakeCacheService:
    def __init__(self) -> None:
        self.client = ScriptCapableFakeRedis()


@pytest.mark.asyncio
async def test_reserve_extraction_slot_fails_closed_when_atomic_reservation_is_unavailable(monkeypatch) -> None:
    tenant_id = str(uuid.uuid4())
    cache_service = FakeCacheService()
    session = MagicMock()
    router = QueueRouter(session=session, cache_service=cache_service)
    await cache_service.client.set(f"tenant:{tenant_id}:plan", "starter", ex=PLAN_CACHE_TTL_SECONDS)
    monkeypatch.setattr(
        cache_service.client,
        "eval",
        AsyncMock(side_effect=RuntimeError("redis transaction unavailable")),
    )

    reservation = await router.reserve_extraction_slot(tenant_id=tenant_id, job_id="job-fail-closed")

    assert reservation is None
    assert await cache_service.client.get(f"tenant_queue_depth:{tenant_id}:starter-extraction") is None


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
    snapshot = await router.inspect_all_queues()
    assert snapshot[STARTER_QUEUE]["admission_attempts_utc_day"] == 1
    assert snapshot[STARTER_QUEUE]["queue_full_utc_day"] == 1
    assert snapshot[STARTER_QUEUE]["queue_full_rate_pct"] == 100.0


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


@pytest.mark.asyncio
async def test_releasing_same_job_twice_does_not_release_another_job() -> None:
    tenant_id = str(uuid.uuid4())
    cache_service = FakeCacheService()
    router = QueueRouter(session=MagicMock(), cache_service=cache_service)
    await cache_service.client.set(f"tenant:{tenant_id}:plan", "enterprise", ex=PLAN_CACHE_TTL_SECONDS)
    await router.reserve_extraction_slot(tenant_id=tenant_id, job_id="job-a")
    await router.reserve_extraction_slot(tenant_id=tenant_id, job_id="job-b")

    await router.release_extraction_slot(tenant_id=tenant_id, queue_name=ENTERPRISE_QUEUE, job_id="job-a")
    await router.release_extraction_slot(tenant_id=tenant_id, queue_name=ENTERPRISE_QUEUE, job_id="job-a")

    assert await cache_service.client.get(
        f"tenant_queue_depth:{tenant_id}:{ENTERPRISE_QUEUE}"
    ) == "1"
    snapshot = await router.inspect_all_queues()
    assert snapshot[ENTERPRISE_QUEUE]["tenant_breakdown"][tenant_id] == 1


@pytest.mark.asyncio
async def test_reserving_same_job_twice_is_idempotent() -> None:
    tenant_id = str(uuid.uuid4())
    cache_service = FakeCacheService()
    router = QueueRouter(session=MagicMock(), cache_service=cache_service)
    await cache_service.client.set(f"tenant:{tenant_id}:plan", "enterprise", ex=PLAN_CACHE_TTL_SECONDS)

    first = await router.reserve_extraction_slot(tenant_id=tenant_id, job_id="job-retried")
    second = await router.reserve_extraction_slot(tenant_id=tenant_id, job_id="job-retried")

    assert first is not None and second is not None
    assert await cache_service.client.get(
        f"tenant_queue_depth:{tenant_id}:{ENTERPRISE_QUEUE}"
    ) == "1"
    snapshot = await router.inspect_all_queues()
    assert snapshot[ENTERPRISE_QUEUE]["tenant_breakdown"][tenant_id] == 1
