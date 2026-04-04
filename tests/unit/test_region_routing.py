from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from starlette.responses import Response

from api.db.database import get_db_session
from api.dependencies import get_cache_service
from api.dependencies import get_qdrant_service
from api.infra.region_pool import RegionConnectionPool
from api.middleware.region import RegionMiddleware


class PassthroughBreaker:
    async def call(self, fn, *args, fallback=None, **kwargs):
        return await fn(*args, **kwargs)


class FakeRedisClient:
    def __init__(self, store: dict[str, str] | None = None) -> None:
        self.store = store or {}

    async def get(self, key: str):
        return self.store.get(key)

    async def set(self, key: str, value: str, *, ex: int | None = None):
        self.store[key] = value
        return True


class FakeCacheService:
    def __init__(self, store: dict[str, str] | None = None) -> None:
        self.client = FakeRedisClient(store)
        self.breaker = PassthroughBreaker()


class FakeSessionContext:
    def __init__(self, session) -> None:
        self.session = session

    async def __aenter__(self):
        return self.session

    async def __aexit__(self, exc_type, exc, tb):
        return False


class FakeRegionPool:
    def __init__(self, *, cache_service, session=None, qdrant_client=None, lookup_region: str = "EU1") -> None:
        self.cache_service = cache_service
        self.session = session or SimpleNamespace(name="eu-session")
        self.qdrant_client = qdrant_client or object()
        self.lookup_region = lookup_region
        self.lookup_tenant_region = AsyncMock(return_value=lookup_region)

    def get_cache_service(self, region_id: str):
        return self.cache_service

    def get_db(self, region_id: str):
        return FakeSessionContext(self.session)

    def get_qdrant(self, region_id: str):
        return self.qdrant_client


class FakeSecretsClient:
    def __init__(self, payloads: dict[str, str]) -> None:
        self.payloads = payloads

    def get_secret_value(self, SecretId: str):
        return {"SecretString": self.payloads[SecretId]}


@pytest.mark.asyncio
async def test_region_middleware_uses_cached_tenant_region() -> None:
    cache_service = FakeCacheService({"tenant:tenant-1:region": "EU1"})
    region_pool = FakeRegionPool(cache_service=cache_service)
    request = SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(region_pool=region_pool, cache_service=cache_service)),
        state=SimpleNamespace(tenant_id="tenant-1"),
    )
    middleware = RegionMiddleware(app=SimpleNamespace())

    async def call_next(_request):
        return Response("ok")

    response = await middleware.dispatch(request, call_next)

    assert response.status_code == 200
    assert request.state.region_id == "EU1"
    region_pool.lookup_tenant_region.assert_not_awaited()


@pytest.mark.asyncio
async def test_region_middleware_caches_db_lookup_result() -> None:
    cache_service = FakeCacheService()
    region_pool = FakeRegionPool(cache_service=cache_service, lookup_region="EU1")
    request = SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(region_pool=region_pool, cache_service=cache_service)),
        state=SimpleNamespace(tenant_id="tenant-2"),
    )
    middleware = RegionMiddleware(app=SimpleNamespace())

    async def call_next(_request):
        return Response("ok")

    await middleware.dispatch(request, call_next)

    assert request.state.region_id == "EU1"
    region_pool.lookup_tenant_region.assert_awaited_once_with("tenant-2")
    assert cache_service.client.store["tenant:tenant-2:region"] == "EU1"


@pytest.mark.asyncio
async def test_request_scoped_dependencies_use_region_pool_resources() -> None:
    cache_service = FakeCacheService()
    session = SimpleNamespace(name="eu-session")
    qdrant_client = object()
    region_pool = FakeRegionPool(
        cache_service=cache_service,
        session=session,
        qdrant_client=qdrant_client,
    )
    request = SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(region_pool=region_pool, cache_service=cache_service)),
        state=SimpleNamespace(region_id="EU1"),
    )

    session_generator = get_db_session(request)
    resolved_session = await anext(session_generator)

    assert resolved_session is session
    assert get_cache_service(request) is cache_service
    assert get_qdrant_service(request).client is qdrant_client

    with pytest.raises(StopAsyncIteration):
        await anext(session_generator)


def test_region_connection_pool_initializes_per_region_resources() -> None:
    secrets_client = FakeSecretsClient(
        {
            "memoryos/regions/IN1/postgres_url": "\"postgresql+asyncpg://memoryos:memoryos123@localhost:5432/memoryos\"",
            "memoryos/regions/IN1/qdrant_url": "\"http://localhost:6333\"",
            "memoryos/regions/IN1/redis_url": "\"redis://localhost:6379/0\"",
            "memoryos/regions/EU1/postgres_url": "\"postgresql+asyncpg://memoryos:memoryos123@localhost:5433/memoryos\"",
            "memoryos/regions/EU1/qdrant_url": "\"http://localhost:7333\"",
            "memoryos/regions/EU1/redis_url": "\"redis://localhost:6380/0\"",
        }
    )
    region_rows = [
        {
            "id": "IN1",
            "aws_region": "ap-south-1",
            "postgres_url_secret": "memoryos/regions/IN1/postgres_url",
            "qdrant_url_secret": "memoryos/regions/IN1/qdrant_url",
            "redis_url_secret": "memoryos/regions/IN1/redis_url",
        },
        {
            "id": "EU1",
            "aws_region": "eu-central-1",
            "postgres_url_secret": "memoryos/regions/EU1/postgres_url",
            "qdrant_url_secret": "memoryos/regions/EU1/qdrant_url",
            "redis_url_secret": "memoryos/regions/EU1/redis_url",
        },
    ]

    pool = RegionConnectionPool(
        app_env="test",
        secrets_client=secrets_client,
        region_rows=region_rows,
    )

    pool.initialize()

    assert pool.get_cache_service("IN1") is not pool.get_cache_service("EU1")
    assert pool.get_qdrant("IN1") is not pool.get_qdrant("EU1")
    assert pool.get_db("IN1") is not pool.get_db("EU1")
