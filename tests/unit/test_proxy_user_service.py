from __future__ import annotations

import uuid
from datetime import UTC
from datetime import datetime
from unittest.mock import AsyncMock
from unittest.mock import MagicMock

import fakeredis.aioredis
import pytest

from api.db.cache import CacheService
from api.db.models import ProxyUser
from api.services.proxy_user_service import ProxyUserBlockedError
from api.services.proxy_user_service import ProxyUserService


class FakeResult:
    def __init__(self, *, scalar_one=None, scalar_one_or_none=None, scalars_all=None):
        self._scalar_one = scalar_one
        self._scalar_one_or_none = scalar_one_or_none
        self._scalars_all = scalars_all or []

    def scalar_one(self):
        return self._scalar_one

    def scalar_one_or_none(self):
        return self._scalar_one_or_none

    def scalars(self):
        return self

    def all(self):
        return list(self._scalars_all)


def make_proxy_user(*, tenant_id: uuid.UUID | None = None, is_blocked: bool = False) -> ProxyUser:
    return ProxyUser(
        id=uuid.uuid4(),
        tenant_id=tenant_id or uuid.uuid4(),
        external_user_id="external-user-1",
        external_user_id_hash="hash-1",
        created_at=datetime.now(UTC),
        last_active_at=datetime.now(UTC),
        memory_count=3,
        metadata_json={"cohort": "2026"},
        is_blocked=is_blocked,
    )


@pytest.mark.asyncio
async def test_resolve_upserts_then_hits_cache() -> None:
    tenant_id = str(uuid.uuid4())
    proxy_user = make_proxy_user(tenant_id=uuid.UUID(tenant_id))

    session = AsyncMock()
    session.execute = AsyncMock(return_value=FakeResult(scalar_one=proxy_user.id))
    session.get = AsyncMock(return_value=proxy_user)
    session.commit = AsyncMock()

    redis_client = fakeredis.aioredis.FakeRedis(decode_responses=True)
    service = ProxyUserService(
        session=session,
        cache_service=CacheService(client=redis_client),
        qdrant_service=MagicMock(),
    )

    first = await service.resolve(tenant_id=tenant_id, external_user_id="external-user-1", metadata={"cohort": "2026"})
    second = await service.resolve(tenant_id=tenant_id, external_user_id="external-user-1")

    assert first.id == proxy_user.id
    assert second.id == proxy_user.id
    assert session.execute.await_count == 1
    assert session.get.await_count == 1
    assert session.commit.await_count == 1
    cache_key = (
        f"proxy_user:{tenant_id}:"
        f"{ProxyUserService.hash_external_user_id(tenant_id, 'external-user-1')}"
    )
    assert await redis_client.ttl(cache_key) == 600


@pytest.mark.asyncio
async def test_resolve_raises_for_blocked_proxy_user_from_cache() -> None:
    tenant_id = str(uuid.uuid4())
    external_user_id = "external-user-2"
    external_user_id_hash = ProxyUserService.hash_external_user_id(tenant_id, external_user_id)
    redis_client = fakeredis.aioredis.FakeRedis(decode_responses=True)
    await redis_client.set(
        f"proxy_user:{tenant_id}:{external_user_id_hash}",
        (
            '{"id":"%s","tenant_id":"%s","external_user_id":"%s","external_user_id_hash":"%s",'
            '"created_at":"2026-03-30T10:00:00+00:00","last_active_at":"2026-03-30T10:01:00+00:00",'
            '"memory_count":0,"metadata":{},"is_blocked":true}'
        )
        % (uuid.uuid4(), tenant_id, external_user_id, external_user_id_hash),
        ex=600,
    )

    service = ProxyUserService(
        session=AsyncMock(),
        cache_service=CacheService(client=redis_client),
        qdrant_service=MagicMock(),
    )

    with pytest.raises(ProxyUserBlockedError):
        await service.resolve(tenant_id=tenant_id, external_user_id=external_user_id)


@pytest.mark.asyncio
async def test_block_invalidates_cache() -> None:
    tenant_uuid = uuid.uuid4()
    proxy_user = make_proxy_user(tenant_id=tenant_uuid)
    proxy_user.external_user_id = "external-user-3"
    proxy_user.external_user_id_hash = ProxyUserService.hash_external_user_id(str(tenant_uuid), proxy_user.external_user_id)

    session = AsyncMock()
    session.execute = AsyncMock(return_value=FakeResult(scalar_one_or_none=proxy_user))
    session.commit = AsyncMock()

    redis_client = fakeredis.aioredis.FakeRedis(decode_responses=True)
    cache_service = CacheService(client=redis_client)
    await redis_client.set(
        f"proxy_user:{tenant_uuid}:{proxy_user.external_user_id_hash}",
        '{"id":"%s"}' % proxy_user.id,
        ex=600,
    )

    service = ProxyUserService(
        session=session,
        cache_service=cache_service,
        qdrant_service=MagicMock(),
    )

    blocked = await service.block(str(tenant_uuid), proxy_user.external_user_id)

    assert blocked is True
    assert proxy_user.is_blocked is True
    assert await redis_client.get(f"proxy_user:{tenant_uuid}:{proxy_user.external_user_id_hash}") is None


@pytest.mark.asyncio
async def test_delete_all_memories_deletes_vectors_and_logs_audit() -> None:
    tenant_uuid = uuid.uuid4()
    proxy_user = make_proxy_user(tenant_id=tenant_uuid)
    proxy_user.external_user_id = "external-user-4"
    proxy_user.external_user_id_hash = ProxyUserService.hash_external_user_id(str(tenant_uuid), proxy_user.external_user_id)

    session = AsyncMock()
    session.execute = AsyncMock(
        side_effect=[
            FakeResult(scalar_one_or_none=proxy_user),
            FakeResult(scalar_one=5),
            FakeResult(scalars_all=[uuid.uuid4() for _ in range(5)]),
            FakeResult(),
        ]
    )
    session.commit = AsyncMock()
    session.add = MagicMock()
    session.delete = AsyncMock()

    service = ProxyUserService(
        session=session,
        cache_service=CacheService(client=fakeredis.aioredis.FakeRedis(decode_responses=True)),
        qdrant_service=MagicMock(),
    )

    removed = await service.delete_all_memories(str(tenant_uuid), proxy_user.external_user_id)

    assert removed == 5
    assert session.add.call_count >= 6
    session.delete.assert_awaited_once_with(proxy_user)
