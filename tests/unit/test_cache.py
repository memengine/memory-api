import fakeredis.aioredis
import asyncio
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
async def test_invalidation_removes_query_and_hot_tier_keys_only_for_user(fake_redis) -> None:
    service = CacheService(client=fake_redis)
    await service.set_retrieval_results("proxy-1", "query", [{"id": "deleted"}])
    await service.set_hot_tier_memory("proxy-1", "deleted", {"id": "deleted"})
    await service.set_retrieval_results("proxy-2", "query", [{"id": "safe"}])
    await service.set_hot_tier_memory("proxy-2", "safe", {"id": "safe"})

    await service.invalidate_user_cache("proxy-1")

    assert await service.get_retrieval_results("proxy-1", "query") is None
    assert await service.get_hot_tier_memories("proxy-1") == []
    assert await service.get_retrieval_results("proxy-2", "query") == [{"id": "safe"}]
    assert await service.get_hot_tier_memories("proxy-2") == [{"id": "safe"}]


def _enable_generation_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APP_ENV", "benchmark")
    monkeypatch.setenv("MEMORYOS_SCALE_DEDICATED", "1")
    monkeypatch.setenv("MEMORYOS_BENCHMARK_PROVIDER", "deterministic")
    monkeypatch.setenv("BENCHMARK_CACHE_INVALIDATION_MODE", "generation-v1")
    monkeypatch.setenv("BENCHMARK_CACHE_NAMESPACE", "v2")


@pytest.mark.asyncio
async def test_generation_cache_is_benchmark_guarded(fake_redis, monkeypatch) -> None:
    monkeypatch.setenv("BENCHMARK_CACHE_INVALIDATION_MODE", "generation-v1")
    monkeypatch.setenv("BENCHMARK_CACHE_NAMESPACE", "v2")

    service = CacheService(client=fake_redis)

    assert service._generation_invalidation is False
    assert service._retrieval_results_key("user-1", "query").startswith("retrieve:")


@pytest.mark.asyncio
async def test_generation_invalidation_makes_prior_values_unreachable_without_scan(
    fake_redis,
    monkeypatch,
) -> None:
    _enable_generation_cache(monkeypatch)
    service = CacheService(client=fake_redis)
    scan_calls = 0

    async def forbidden_scan(*_args, **_kwargs):
        nonlocal scan_calls
        scan_calls += 1
        if False:
            yield None

    fake_redis.scan_iter = forbidden_scan
    await service.set_hot_memories("user-1", [{"id": "list-old"}])
    await service.set_retrieval_results("user-1", "query", [{"id": "result-old"}])
    await service.set_hot_tier_memory("user-1", "memory-old", {"id": "memory-old"})

    await service.invalidate_user_cache("user-1")

    assert await service.get_hot_memories("user-1") is None
    assert await service.get_retrieval_results("user-1", "query") is None
    assert await service.get_hot_tier_memories("user-1") == []
    assert await fake_redis.get(service._generation_key("user-1")) == "1"
    assert scan_calls == 0


@pytest.mark.asyncio
async def test_generation_cache_keeps_other_identities_and_new_values(fake_redis, monkeypatch) -> None:
    _enable_generation_cache(monkeypatch)
    service = CacheService(client=fake_redis)
    await service.set_retrieval_results("user-1", "query", [{"id": "old"}])
    await service.set_retrieval_results("user-2", "query", [{"id": "safe"}])

    await service.invalidate_user_cache("user-1")
    await service.set_retrieval_results("user-1", "query", [{"id": "new"}])

    assert await service.get_retrieval_results("user-1", "query") == [{"id": "new"}]
    assert await service.get_retrieval_results("user-2", "query") == [{"id": "safe"}]


@pytest.mark.asyncio
async def test_generation_hot_tier_uses_one_hash_and_refreshes_ttl(fake_redis, monkeypatch) -> None:
    _enable_generation_cache(monkeypatch)
    service = CacheService(client=fake_redis)

    await service.set_hot_tier_memory("user-1", "memory-1", {"id": "memory-1"}, ttl=300)
    await service.set_hot_tier_memory("user-1", "memory-2", {"id": "memory-2"}, ttl=300)

    key = service._generation_hot_tier_key("user-1", 0)
    assert sorted(item["id"] for item in await service.get_hot_tier_memories("user-1")) == [
        "memory-1",
        "memory-2",
    ]
    assert await fake_redis.hlen(key) == 2
    assert 0 < await fake_redis.ttl(key) <= 300


@pytest.mark.asyncio
async def test_concurrent_generation_invalidations_are_monotonic(fake_redis, monkeypatch) -> None:
    _enable_generation_cache(monkeypatch)
    service = CacheService(client=fake_redis)
    await service.set_retrieval_results("user-1", "query", [{"id": "old"}])

    await asyncio.gather(*(service.invalidate_user_cache("user-1") for _ in range(20)))

    assert await fake_redis.get(service._generation_key("user-1")) == "20"
    assert await service.get_retrieval_results("user-1", "query") is None


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
    await service.set_idempotent_response(
        "idem-1",
        response_payload,
        ttl=86400,
        scope="tenant:tenant-1",
    )

    assert await service.get_job_status("job-1") == job_payload
    assert (
        await service.get_idempotent_response("idem-1", scope="tenant:tenant-1")
        == response_payload
    )
    assert await service.get_idempotent_response("idem-1", scope="tenant:tenant-2") is None
    assert 0 < await fake_redis.ttl(service._job_status_key("job-1")) <= 3600
    assert (
        0
        < await fake_redis.ttl(
            service._idempotency_key("idem-1", scope="tenant:tenant-1")
        )
        <= 86400
    )


def test_redis_key_patterns_follow_contract_shape() -> None:
    assert CacheService._hot_memories_key("user-123") == "user:user-123:hot_memories"
    assert CacheService._hot_tier_memory_key("proxy-123", "memory-123") == "hot_memory:proxy-123:memory-123"
    assert CacheService._rate_limit_key("hashed-key", 60).startswith("rate:")
    assert CacheService._rate_limit_key("hashed-key", 60).endswith(":60")
    assert CacheService._job_status_key("job-123") == "job:job-123:status"
    first = CacheService._idempotency_key("idem-123", scope="tenant:one")
    second = CacheService._idempotency_key("idem-123", scope="tenant:two")
    assert first.startswith("idempotency:")
    assert ":memory_add:" in first
    assert "idem-123" not in first
    assert first != second


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
async def test_breaker_owned_cache_failure_falls_back_without_force_open() -> None:
    class TrackingBreaker:
        def __init__(self) -> None:
            self.force_open_calls = 0

        async def call(self, fn, *args, fallback=None, **kwargs):
            try:
                return await fn(*args, **kwargs)
            except RedisConnectionError:
                raise

        def force_open(self) -> None:
            self.force_open_calls += 1

    service = CacheService(client=BrokenRedis())
    service.breaker = TrackingBreaker()

    assert await service.get_hot_memories("user-1") is None
    assert service.breaker.force_open_calls == 0

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
