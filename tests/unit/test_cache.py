import fakeredis.aioredis
import pytest
import pytest_asyncio
from redis.exceptions import ConnectionError as RedisConnectionError

from api.db.cache import CacheService


@pytest_asyncio.fixture
async def fake_redis():
    client = fakeredis.aioredis.FakeRedis(decode_responses=True)
    yield client
    await client.flushall()
    await client.aclose()


@pytest.mark.asyncio
async def test_hot_memories_cache_hit_and_miss(fake_redis) -> None:
    service = CacheService(client=fake_redis)

    assert await service.get_hot_memories("user-1") is None

    memories = [{"id": "memory-1", "content": "hello"}]
    await service.set_hot_memories("user-1", memories, ttl=300)

    assert await service.get_hot_memories("user-1") == memories


@pytest.mark.asyncio
async def test_set_hot_memories_sets_ttl(fake_redis) -> None:
    service = CacheService(client=fake_redis)

    await service.set_hot_memories("user-1", [{"id": "memory-1"}], ttl=300)

    ttl = await fake_redis.ttl(service._hot_memories_key("user-1"))
    assert 0 < ttl <= 300


@pytest.mark.asyncio
async def test_invalidate_user_cache_removes_hot_memories(fake_redis) -> None:
    service = CacheService(client=fake_redis)
    await service.set_hot_memories("user-1", [{"id": "memory-1"}], ttl=300)

    await service.invalidate_user_cache("user-1")

    assert await service.get_hot_memories("user-1") is None


@pytest.mark.asyncio
async def test_increment_rate_counter_tracks_count_and_ttl(fake_redis) -> None:
    service = CacheService(client=fake_redis)

    first = await service.increment_rate_counter("hashed-key", window_seconds=60)
    second = await service.increment_rate_counter("hashed-key", window_seconds=60)

    assert first == 1
    assert second == 2
    assert await service.get_rate_count("hashed-key", window_seconds=60) == 2

    ttl = await fake_redis.ttl(service._rate_limit_key("hashed-key", 60))
    assert 0 < ttl <= 60


@pytest.mark.asyncio
async def test_job_status_and_idempotency_responses_use_ttl_backed_json_keys(fake_redis) -> None:
    service = CacheService(client=fake_redis)

    job_payload = {"job_id": "job-1", "status": "queued", "memories_created": 0}
    response_payload = {"job_id": "job-1", "status": "queued"}

    await service.set_job_status("job-1", job_payload, ttl=3600)
    await service.set_idempotent_response("idem-1", response_payload, ttl=86400)

    assert await service.get_job_status("job-1") == job_payload
    assert await service.get_idempotent_response("idem-1") == response_payload
    assert 0 < await fake_redis.ttl(service._job_status_key("job-1")) <= 3600
    assert 0 < await fake_redis.ttl(service._idempotency_key("idem-1")) <= 86400


def test_redis_key_patterns_follow_contract_shape() -> None:
    assert CacheService._hot_memories_key("user-123") == "user:user-123:hot_memories"
    assert CacheService._hot_tier_memory_key("proxy-123", "memory-123") == "hot_memory:proxy-123:memory-123"
    assert CacheService._rate_limit_key("hashed-key", 60).startswith("rate:")
    assert CacheService._rate_limit_key("hashed-key", 60).endswith(":60")
    assert CacheService._job_status_key("job-123") == "job:job-123:status"
    assert CacheService._idempotency_key("idem-123") == "idempotency:idem-123"


@pytest.mark.asyncio
async def test_hot_memories_returns_none_when_json_is_invalid(fake_redis) -> None:
    service = CacheService(client=fake_redis)
    await fake_redis.set(service._hot_memories_key("user-1"), "not-json", ex=300)

    assert await service.get_hot_memories("user-1") is None


@pytest.mark.asyncio
async def test_hot_tier_memory_cache_round_trip(fake_redis) -> None:
    service = CacheService(client=fake_redis)
    payload = {"id": "memory-1", "content": "important", "final_score": 1.0}

    await service.set_hot_tier_memory("proxy-1", "memory-1", payload, ttl=300)

    assert await service.get_hot_tier_memories("proxy-1") == [payload]
    assert 0 < await fake_redis.ttl(service._hot_tier_memory_key("proxy-1", "memory-1")) <= 300


@pytest.mark.asyncio
async def test_lifecycle_reports_cache_round_trip(fake_redis) -> None:
    service = CacheService(client=fake_redis)
    report = {"tenant_id": "tenant-1", "archived_count": 2}

    await service.set_lifecycle_report("tenant-1", report, ttl=300)

    assert await service.get_lifecycle_reports() == [report]


class BrokenRedis:
    async def get(self, *_args, **_kwargs):
        raise RedisConnectionError("redis unavailable")

    async def set(self, *_args, **_kwargs):
        raise RedisConnectionError("redis unavailable")

    async def delete(self, *_args, **_kwargs):
        raise RedisConnectionError("redis unavailable")

    async def incr(self, *_args, **_kwargs):
        raise RedisConnectionError("redis unavailable")

    async def ttl(self, *_args, **_kwargs):
        raise RedisConnectionError("redis unavailable")

    async def expire(self, *_args, **_kwargs):
        raise RedisConnectionError("redis unavailable")


@pytest.mark.asyncio
async def test_cache_service_handles_connection_errors_gracefully() -> None:
    service = CacheService(client=BrokenRedis())

    assert await service.get_hot_memories("user-1") is None
    await service.set_hot_memories("user-1", [{"id": "memory-1"}])
    await service.invalidate_user_cache("user-1")
    assert await service.increment_rate_counter("hashed-key") == 0
    assert await service.get_rate_count("hashed-key") == 0
    assert await service.get_job_status("job-1") is None
    await service.set_job_status("job-1", {"status": "queued"})
    assert await service.get_idempotent_response("idem-1") is None
    await service.set_idempotent_response("idem-1", {"job_id": "job-1"})
