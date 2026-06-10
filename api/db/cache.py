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
HOT_TIER_PREFIX = "hot_memory"
LIFECYCLE_REPORT_PREFIX = "lifecycle_report"
LIFECYCLE_REPORT_INDEX = "lifecycle_report:index"
RATE_LIMIT_PREFIX = "rate"
JOB_STATUS_PREFIX = "job"
IDEMPOTENCY_PREFIX = "idempotency"
PROVIDER_USAGE_PREFIX = "provider_usage"
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
    def _retrieval_results_key(user_id: str, cache_context: str) -> str:
        context_hash = hashlib.sha256(cache_context.encode("utf-8")).hexdigest()[:24]
        return f"retrieve:{user_id}:{context_hash}"

    @staticmethod
    def _hot_tier_memory_key(proxy_user_id: str, memory_id: str) -> str:
        return f"{HOT_TIER_PREFIX}:{proxy_user_id}:{memory_id}"

    @staticmethod
    def _hot_tier_memory_pattern(proxy_user_id: str) -> str:
        return f"{HOT_TIER_PREFIX}:{proxy_user_id}:*"

    @staticmethod
    def _lifecycle_report_key(tenant_id: str) -> str:
        return f"{LIFECYCLE_REPORT_PREFIX}:{tenant_id}"

    @staticmethod
    def _rate_limit_key(key_hash: str, window_seconds: int) -> str:
        key_prefix = hashlib.sha256(key_hash.encode("utf-8")).hexdigest()[:12]
        return f"{RATE_LIMIT_PREFIX}:{key_prefix}:{window_seconds}"

    @staticmethod
    def _job_status_key(job_id: str) -> str:
        return f"{JOB_STATUS_PREFIX}:{job_id}:status"

    @staticmethod
    def _idempotency_key(
        idempotency_key: str,
        *,
        scope: str = "legacy",
        operation: str = "memory_add",
    ) -> str:
        scope_hash = hashlib.sha256(scope.encode("utf-8")).hexdigest()[:20]
        key_hash = hashlib.sha256(idempotency_key.encode("utf-8")).hexdigest()
        return f"{IDEMPOTENCY_PREFIX}:{scope_hash}:{operation}:{key_hash}"

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

    async def get_retrieval_results(self, user_id: str, cache_context: str) -> list[dict[str, Any]] | None:
        try:
            cached_value = await self.breaker.call(
                self.client.get,
                self._retrieval_results_key(user_id, cache_context),
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

    async def set_retrieval_results(
        self,
        user_id: str,
        cache_context: str,
        memories: list[dict[str, Any]],
        ttl: int = 60,
    ) -> None:
        if ttl <= 0:
            raise ValueError("TTL must be greater than zero.")
        try:
            await self.breaker.call(
                self.client.set,
                self._retrieval_results_key(user_id, cache_context),
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
            retrieval_keys: list[str] = []
            async for key in self.client.scan_iter(f"retrieve:{user_id}:*"):
                retrieval_keys.append(str(key))
            if retrieval_keys:
                await self.breaker.call(
                    self.client.delete,
                    *retrieval_keys,
                    fallback=lambda: on_redis_open(None),
                )
        except REDIS_FAILURES:
            self._mark_redis_unavailable()
            return None

    async def set_hot_tier_memory(
        self,
        proxy_user_id: str,
        memory_id: str,
        memory: dict[str, Any],
        ttl: int = 86400,
    ) -> None:
        if ttl <= 0:
            raise ValueError("TTL must be greater than zero.")
        try:
            await self.breaker.call(
                self.client.set,
                self._hot_tier_memory_key(proxy_user_id, memory_id),
                json.dumps(memory, default=str),
                ex=ttl,
                fallback=lambda: on_redis_open(None),
            )
        except Exception:
            self._mark_redis_unavailable()
            return None

    async def get_hot_tier_memories(self, proxy_user_id: str) -> list[dict[str, Any]]:
        memories: list[dict[str, Any]] = []
        try:
            keys = []
            async for key in self.client.scan_iter(self._hot_tier_memory_pattern(proxy_user_id)):
                keys.append(key)
            if not keys:
                return []
            raw_values = await self.breaker.call(
                self.client.mget,
                keys,
                fallback=lambda: on_redis_open([]),
            )
        except Exception:
            self._mark_redis_unavailable()
            return []

        for raw_value in raw_values or []:
            if raw_value is None:
                continue
            try:
                payload = json.loads(raw_value)
            except json.JSONDecodeError:
                continue
            if isinstance(payload, dict):
                memories.append(payload)
        return memories

    async def set_lifecycle_report(
        self,
        tenant_id: str,
        report: dict[str, Any],
        ttl: int = 604800,
    ) -> None:
        key = self._lifecycle_report_key(tenant_id)
        try:
            await self._set_json(key, report, ttl=ttl)
            await self.breaker.call(
                self.client.sadd,
                LIFECYCLE_REPORT_INDEX,
                str(tenant_id),
                fallback=lambda: on_redis_open(None),
            )
        except Exception:
            self._mark_redis_unavailable()
            return None

    async def get_lifecycle_reports(self) -> list[dict[str, Any]]:
        try:
            tenant_ids = await self.breaker.call(
                self.client.smembers,
                LIFECYCLE_REPORT_INDEX,
                fallback=lambda: on_redis_open([]),
            )
        except Exception:
            self._mark_redis_unavailable()
            return []

        reports: list[dict[str, Any]] = []
        for tenant_id in sorted(tenant_ids or []):
            report = await self._get_json(self._lifecycle_report_key(str(tenant_id)))
            if report is not None:
                reports.append(report)
        return reports

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

    async def increment_provider_usage(self, provider: str, hour_bucket: str, ttl: int = 7200) -> int:
        key = f"{PROVIDER_USAGE_PREFIX}:{provider}:{hour_bucket}"
        try:
            new_count = int(
                await self.breaker.call(
                    self.client.incr,
                    key,
                    fallback=lambda: on_redis_open(0),
                )
            )
            await self.breaker.call(
                self.client.expire,
                key,
                ttl,
                fallback=lambda: on_redis_open(None),
            )
            return new_count
        except REDIS_FAILURES:
            self._mark_redis_unavailable()
            return 0

    async def get_provider_usage(self, hour_bucket: str, providers: list[str]) -> dict[str, int]:
        keys = [f"{PROVIDER_USAGE_PREFIX}:{provider}:{hour_bucket}" for provider in providers]
        try:
            raw_values = await self.breaker.call(
                self.client.mget,
                keys,
                fallback=lambda: on_redis_open([0 for _ in keys]),
            )
        except REDIS_FAILURES:
            self._mark_redis_unavailable()
            return {provider: 0 for provider in providers}

        return {
            provider: int(raw_values[index] or 0)
            for index, provider in enumerate(providers)
        }

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

    async def get_idempotent_response(
        self,
        idempotency_key: str,
        *,
        scope: str = "legacy",
        operation: str = "memory_add",
    ) -> dict[str, Any] | None:
        return await self._get_json(
            self._idempotency_key(idempotency_key, scope=scope, operation=operation)
        )

    async def set_idempotent_response(
        self,
        idempotency_key: str,
        response_payload: dict[str, Any],
        ttl: int = 86400,
        *,
        scope: str = "legacy",
        operation: str = "memory_add",
    ) -> None:
        await self._set_json(
            self._idempotency_key(idempotency_key, scope=scope, operation=operation),
            response_payload,
            ttl=ttl,
        )

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
