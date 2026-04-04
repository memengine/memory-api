from __future__ import annotations

import json
import os
import hashlib
from typing import Any

import redis.asyncio as redis
from redis.asyncio.retry import Retry
from redis.asyncio import Redis
from redis.backoff import NoBackoff
from redis.exceptions import ConnectionError as RedisConnectionError
from redis.exceptions import TimeoutError as RedisTimeoutError

from api.infra.circuit_breaker_registry import CircuitBreakerRegistry
from api.infra.fallbacks import on_redis_open
from api.settings import get_settings
HOT_MEMORIES_SUFFIX = "hot_memories"
RATE_LIMIT_PREFIX = "rate"
JOB_STATUS_PREFIX = "job"
IDEMPOTENCY_PREFIX = "idempotency"
REDIS_FAILURES = (RedisConnectionError, RedisTimeoutError)


def get_redis_url(redis_url: str | None = None) -> str:
    resolved_url = redis_url or os.getenv("REDIS_URL") or get_settings().redis_url
    if not resolved_url:
        raise RuntimeError("REDIS_URL is required.")
    return resolved_url


class _DirectRedisBreaker:
    async def call(self, fn, *args, fallback=None, **kwargs):
        return await fn(*args, **kwargs)


class CacheService:
    def __init__(
        self,
        redis_url: str | None = None,
        client: Redis | None = None,
        *,
        use_direct_breaker: bool | None = None,
    ) -> None:
        self.client = client or redis.from_url(
            get_redis_url(redis_url),
            encoding="utf-8",
            decode_responses=True,
            socket_connect_timeout=0.1,
            socket_timeout=0.1,
            retry=Retry(NoBackoff(), 0),
            retry_on_timeout=False,
        )
        direct_breaker = bool(client is not None) if use_direct_breaker is None else bool(use_direct_breaker)
        self.breaker = (
            _DirectRedisBreaker()
            if direct_breaker
            else CircuitBreakerRegistry.get_instance().redis_cb
        )

    def _mark_redis_unavailable(self) -> None:
        force_open = getattr(self.breaker, "force_open", None)
        if callable(force_open):
            try:
                force_open()
            except Exception:
                return None

    @staticmethod
    def _hot_memories_key(user_id: str) -> str:
        return f"user:{user_id}:{HOT_MEMORIES_SUFFIX}"

    @staticmethod
    def _rate_limit_key(key_hash: str, window_seconds: int) -> str:
        key_prefix = hashlib.sha256(key_hash.encode("utf-8")).hexdigest()[:12]
        return f"{RATE_LIMIT_PREFIX}:{key_prefix}:{window_seconds}"

    @staticmethod
    def _job_status_key(job_id: str) -> str:
        return f"{JOB_STATUS_PREFIX}:{job_id}:status"

    @staticmethod
    def _idempotency_key(idempotency_key: str) -> str:
        return f"{IDEMPOTENCY_PREFIX}:{idempotency_key}"

    async def get_hot_memories(self, user_id: str) -> list[dict[str, Any]] | None:
        try:
            cached_value = await self.breaker.call(
                self.client.get,
                self._hot_memories_key(user_id),
                fallback=lambda: on_redis_open(None),
            )
        except REDIS_FAILURES:
            self._mark_redis_unavailable()
            return None

        if cached_value is None:
            return None

        try:
            data = json.loads(cached_value)
        except json.JSONDecodeError:
            return None

        return data if isinstance(data, list) else None

    async def set_hot_memories(
        self,
        user_id: str,
        memories: list[dict[str, Any]],
        ttl: int = 300,
    ) -> None:
        if ttl <= 0:
            raise ValueError("TTL must be greater than zero.")
        try:
            await self.breaker.call(
                self.client.set,
                self._hot_memories_key(user_id),
                json.dumps(memories, default=str),
                ex=ttl,
                fallback=lambda: on_redis_open(None),
            )
        except REDIS_FAILURES:
            self._mark_redis_unavailable()
            return None

    async def invalidate_user_cache(self, user_id: str) -> None:
        try:
            await self.breaker.call(
                self.client.delete,
                self._hot_memories_key(user_id),
                fallback=lambda: on_redis_open(None),
            )
        except REDIS_FAILURES:
            self._mark_redis_unavailable()
            return None

    async def increment_rate_counter(self, api_key: str, window_seconds: int = 60) -> int:
        key = self._rate_limit_key(api_key, window_seconds)

        try:
            new_count = int(
                await self.breaker.call(
                    self.client.incr,
                    key,
                    fallback=lambda: on_redis_open(0),
                )
            )
            ttl = await self.breaker.call(
                self.client.ttl,
                key,
                fallback=lambda: on_redis_open(-1),
            )
            if ttl is None or ttl < 0:
                await self.breaker.call(
                    self.client.expire,
                    key,
                    window_seconds,
                    fallback=lambda: on_redis_open(None),
                )
            return new_count
        except REDIS_FAILURES:
            self._mark_redis_unavailable()
            return 0

    async def get_rate_count(self, api_key: str, window_seconds: int = 60) -> int:
        key = self._rate_limit_key(api_key, window_seconds)

        try:
            raw_value = await self.breaker.call(
                self.client.get,
                key,
                fallback=lambda: on_redis_open(None),
            )
        except REDIS_FAILURES:
            self._mark_redis_unavailable()
            return 0

        return int(raw_value) if raw_value is not None else 0

    async def get_job_status(self, job_id: str) -> dict[str, Any] | None:
        return await self._get_json(self._job_status_key(job_id))

    async def set_job_status(self, job_id: str, status_payload: dict[str, Any], ttl: int = 3600) -> None:
        await self._set_json(self._job_status_key(job_id), status_payload, ttl=ttl)

    async def get_idempotent_response(self, idempotency_key: str) -> dict[str, Any] | None:
        return await self._get_json(self._idempotency_key(idempotency_key))

    async def set_idempotent_response(
        self,
        idempotency_key: str,
        response_payload: dict[str, Any],
        ttl: int = 86400,
    ) -> None:
        await self._set_json(self._idempotency_key(idempotency_key), response_payload, ttl=ttl)

    async def _get_json(self, key: str) -> dict[str, Any] | None:
        try:
            cached_value = await self.breaker.call(
                self.client.get,
                key,
                fallback=lambda: on_redis_open(None),
            )
        except REDIS_FAILURES:
            self._mark_redis_unavailable()
            return None

        if cached_value is None:
            return None

        try:
            data = json.loads(cached_value)
        except json.JSONDecodeError:
            return None

        return data if isinstance(data, dict) else None

    async def _set_json(self, key: str, payload: dict[str, Any], ttl: int) -> None:
        if ttl <= 0:
            raise ValueError("TTL must be greater than zero.")

        try:
            await self.breaker.call(
                self.client.set,
                key,
                json.dumps(payload, default=str),
                ex=ttl,
                fallback=lambda: on_redis_open(None),
            )
        except REDIS_FAILURES:
            self._mark_redis_unavailable()
            return None
