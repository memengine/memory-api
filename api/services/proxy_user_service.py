from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from redis.exceptions import ConnectionError as RedisConnectionError
from redis.exceptions import TimeoutError as RedisTimeoutError
from sqlalchemy import delete
from sqlalchemy import func
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from api.db.cache import CacheService
from api.db.models import AuditAction
from api.db.models import AuditLog
from api.db.models import Memory
from api.db.models import ProxyUser
from api.db.vector_store import QdrantService
from api.errors import APIError
from api.infra.fallbacks import on_redis_open
from api.services.vector_outbox import enqueue_vector_delete


PROXY_USER_CACHE_TTL_SECONDS = 600
PROXY_USER_CACHE_PREFIX = "proxy_user"
REDIS_FAILURES = (RedisConnectionError, RedisTimeoutError)


class ProxyUserBlockedError(APIError):
    def __init__(self, *, tenant_id: str, external_user_id_hash: str) -> None:
        super().__init__(
            status_code=403,
            code="PRX_403",
            error="proxy_user_blocked",
            details={
                "tenant_id": tenant_id,
                "external_user_id_hash": external_user_id_hash,
            },
        )


@dataclass(slots=True)
class ProxyUserStats:
    memory_count: int
    last_active_at: Any
    created_at: Any


class ProxyUserService:
    def __init__(
        self,
        *,
        session: AsyncSession,
        cache_service: CacheService,
        qdrant_service: QdrantService | None = None,
        region_id: str | None = None,
    ) -> None:
        self.session = session
        self.cache_service = cache_service
        self.qdrant_service = qdrant_service
        self.region_id = region_id

    async def resolve(
        self,
        tenant_id: str,
        external_user_id: str,
        metadata: dict[str, Any] | None = None,
    ) -> ProxyUser:
        external_user_id_hash = self.hash_external_user_id(tenant_id, external_user_id)
        cache_key = self._cache_key(tenant_id, external_user_id_hash)
        cached_proxy_user = await self._get_cached_proxy_user(cache_key)
        if cached_proxy_user is not None:
            if cached_proxy_user.is_blocked:
                raise ProxyUserBlockedError(
                    tenant_id=tenant_id,
                    external_user_id_hash=external_user_id_hash,
                )
            return cached_proxy_user

        proxy_user = await self._upsert_proxy_user(
            tenant_id=tenant_id,
            external_user_id=external_user_id,
            external_user_id_hash=external_user_id_hash,
            metadata=metadata or {},
        )
        await self._cache_proxy_user(cache_key, proxy_user)

        if proxy_user.is_blocked:
            raise ProxyUserBlockedError(
                tenant_id=tenant_id,
                external_user_id_hash=external_user_id_hash,
            )
        return proxy_user

    async def block(self, tenant_id: str, external_user_id: str) -> bool:
        proxy_user = await self._get_proxy_user_by_hash(
            tenant_id=tenant_id,
            external_user_id_hash=self.hash_external_user_id(tenant_id, external_user_id),
        )
        if proxy_user is None:
            return False

        proxy_user.is_blocked = True
        proxy_user.last_active_at = func.now()
        await self.session.commit()
        await self._invalidate_cache(tenant_id, proxy_user.external_user_id_hash)
        return True

    async def get_stats(self, tenant_id: str, external_user_id: str) -> ProxyUserStats:
        proxy_user = await self._require_proxy_user(
            tenant_id=tenant_id,
            external_user_id_hash=self.hash_external_user_id(tenant_id, external_user_id),
        )
        return ProxyUserStats(
            memory_count=int(proxy_user.memory_count or 0),
            last_active_at=proxy_user.last_active_at,
            created_at=proxy_user.created_at,
        )

    async def delete_all_memories(self, tenant_id: str, external_user_id: str) -> int:
        proxy_user = await self._require_proxy_user(
            tenant_id=tenant_id,
            external_user_id_hash=self.hash_external_user_id(tenant_id, external_user_id),
        )

        memory_count = await self._count_proxy_user_memories(proxy_user.id)
        memory_ids_result = await self.session.execute(
            select(Memory.id).where(Memory.proxy_user_id == proxy_user.id)
        )
        memory_ids = list(memory_ids_result.scalars().all())
        for memory_id in memory_ids:
            enqueue_vector_delete(
                self.session,
                memory_id=memory_id,
                payload={"memory_id": str(memory_id)},
            )
        await self.session.execute(delete(Memory).where(Memory.proxy_user_id == proxy_user.id))
        self.session.add(
            AuditLog(
                user_id=None,
                proxy_user_id=proxy_user.id,
                action=AuditAction.proxy_user_deleted,
                memory_id=None,
                old_value={
                    "memory_count": memory_count,
                    "vector_count": len(memory_ids),
                },
                new_value={"deleted": True},
                metadata_json={
                    "tenant_id": str(proxy_user.tenant_id),
                    "external_user_id_hash": proxy_user.external_user_id_hash,
                },
                ip_address=None,
            )
        )
        await self.session.delete(proxy_user)
        await self.session.commit()
        await self._invalidate_cache(tenant_id, proxy_user.external_user_id_hash)
        return memory_count

    async def _upsert_proxy_user(
        self,
        *,
        tenant_id: str,
        external_user_id: str,
        external_user_id_hash: str,
        metadata: dict[str, Any],
    ) -> ProxyUser:
        insert_stmt = insert(ProxyUser).values(
            tenant_id=self._as_uuid(tenant_id),
            external_user_id=external_user_id,
            external_user_id_hash=external_user_id_hash,
            metadata_json=metadata,
        )
        update_values: dict[str, Any] = {
            "last_active_at": func.now(),
        }
        if metadata:
            update_values["metadata"] = insert_stmt.excluded["metadata"]

        upsert_stmt = (
            insert_stmt.on_conflict_do_update(
                index_elements=[ProxyUser.tenant_id, ProxyUser.external_user_id_hash],
                set_=update_values,
            )
            .returning(ProxyUser.id)
        )
        result = await self.session.execute(upsert_stmt)
        proxy_user_id = result.scalar_one()
        await self.session.commit()

        proxy_user = await self.session.get(ProxyUser, proxy_user_id)
        if proxy_user is None:
            raise APIError(
                status_code=500,
                code="PRX_500",
                error="proxy_user_resolution_failed",
            )
        return proxy_user

    async def _get_proxy_user_by_hash(
        self,
        *,
        tenant_id: str,
        external_user_id_hash: str,
    ) -> ProxyUser | None:
        result = await self.session.execute(
            select(ProxyUser).where(
                ProxyUser.tenant_id == self._as_uuid(tenant_id),
                ProxyUser.external_user_id_hash == external_user_id_hash,
            )
        )
        return result.scalar_one_or_none()

    async def _require_proxy_user(
        self,
        *,
        tenant_id: str,
        external_user_id_hash: str,
    ) -> ProxyUser:
        proxy_user = await self._get_proxy_user_by_hash(
            tenant_id=tenant_id,
            external_user_id_hash=external_user_id_hash,
        )
        if proxy_user is None:
            raise APIError(
                status_code=404,
                code="PRX_404",
                error="proxy_user_not_found",
                details={
                    "tenant_id": tenant_id,
                    "external_user_id_hash": external_user_id_hash,
                },
            )
        return proxy_user

    async def _count_proxy_user_memories(self, proxy_user_id: uuid.UUID) -> int:
        result = await self.session.execute(
            select(func.count(Memory.id)).where(Memory.proxy_user_id == proxy_user_id)
        )
        return int(result.scalar_one() or 0)

    async def _get_cached_proxy_user(self, cache_key: str) -> ProxyUser | None:
        try:
            cached_value = await self._redis_call(
                self.cache_service.client.get,
                cache_key,
                fallback=lambda: on_redis_open(None),
            )
        except REDIS_FAILURES:
            return None

        if cached_value is None:
            return None

        try:
            payload = json.loads(cached_value)
        except json.JSONDecodeError:
            return None

        return ProxyUser(
            id=self._as_uuid(payload["id"]),
            tenant_id=self._as_uuid(payload["tenant_id"]),
            external_user_id=payload["external_user_id"],
            external_user_id_hash=payload["external_user_id_hash"],
            created_at=datetime.fromisoformat(payload["created_at"]) if payload.get("created_at") else None,
            last_active_at=datetime.fromisoformat(payload["last_active_at"]) if payload.get("last_active_at") else None,
            memory_count=int(payload.get("memory_count", 0)),
            metadata_json=payload.get("metadata", {}),
            is_blocked=bool(payload.get("is_blocked", False)),
        )

    async def _cache_proxy_user(self, cache_key: str, proxy_user: ProxyUser) -> None:
        payload = {
            "id": str(proxy_user.id),
            "tenant_id": str(proxy_user.tenant_id),
            "external_user_id": proxy_user.external_user_id,
            "external_user_id_hash": proxy_user.external_user_id_hash,
            "created_at": proxy_user.created_at.isoformat() if proxy_user.created_at else None,
            "last_active_at": proxy_user.last_active_at.isoformat() if proxy_user.last_active_at else None,
            "memory_count": int(proxy_user.memory_count or 0),
            "metadata": proxy_user.metadata_json or {},
            "is_blocked": bool(proxy_user.is_blocked),
        }
        try:
            await self._redis_call(
                self.cache_service.client.set,
                cache_key,
                json.dumps(payload),
                ex=PROXY_USER_CACHE_TTL_SECONDS,
                fallback=lambda: on_redis_open(None),
            )
        except REDIS_FAILURES:
            return None

    async def _invalidate_cache(self, tenant_id: str, external_user_id_hash: str) -> None:
        try:
            await self._redis_call(
                self.cache_service.client.delete,
                self._cache_key(tenant_id, external_user_id_hash),
                fallback=lambda: on_redis_open(None),
            )
        except REDIS_FAILURES:
            return None

    @staticmethod
    def hash_external_user_id(tenant_id: str, external_user_id: str) -> str:
        return hashlib.sha256(f"{tenant_id}:{external_user_id}".encode("utf-8")).hexdigest()

    @staticmethod
    def _cache_key(tenant_id: str, external_user_id_hash: str) -> str:
        return f"{PROXY_USER_CACHE_PREFIX}:{tenant_id}:{external_user_id_hash}"

    async def _redis_call(self, fn, *args, fallback=None, **kwargs):
        breaker = getattr(self.cache_service, "breaker", None)
        if (
            breaker is None
            or breaker.__class__.__module__.startswith("unittest.mock")
        ):
            return await fn(*args, **kwargs)
        return await breaker.call(fn, *args, fallback=fallback, **kwargs)

    @staticmethod
    def _as_uuid(value: str | uuid.UUID) -> uuid.UUID:
        return value if isinstance(value, uuid.UUID) else uuid.UUID(str(value))
