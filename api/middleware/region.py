from __future__ import annotations

import logging
from typing import Any
from typing import Callable

from redis.exceptions import ConnectionError as RedisConnectionError
from redis.exceptions import TimeoutError as RedisTimeoutError
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from api.infra.fallbacks import on_redis_open
from api.infra.region_pool import DEFAULT_REGION_ID


LOGGER = logging.getLogger("memoryos.region")
REGION_CACHE_TTL_SECONDS = 3600
REDIS_FAILURES = (RedisConnectionError, RedisTimeoutError)


class RegionMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: Callable[..., Any]) -> Response:
        request.state.region_id = DEFAULT_REGION_ID
        tenant_id = getattr(request.state, "tenant_id", None)
        if tenant_id:
            request.state.region_id = await self._resolve_region_id(
                request=request,
                tenant_id=str(tenant_id),
            )
        return await call_next(request)

    async def _resolve_region_id(self, *, request: Request, tenant_id: str) -> str:
        region_pool = getattr(request.app.state, "region_pool", None)
        if region_pool is None:
            return DEFAULT_REGION_ID

        cache_service = getattr(request.app.state, "cache_service", None)
        cache_key = f"tenant:{tenant_id}:region"
        if cache_service is not None:
            cached_region = await self._read_cache(cache_service, cache_key)
            if cached_region:
                LOGGER.info(
                    "tenant_region_resolved tenant_id=%s region_id=%s source=cache",
                    tenant_id,
                    cached_region,
                )
                return cached_region

        try:
            region_id = await region_pool.lookup_tenant_region(tenant_id)
        except Exception as exc:
            LOGGER.warning("tenant region lookup failed for %s: %s", tenant_id, exc)
            return DEFAULT_REGION_ID

        if cache_service is not None:
            await self._write_cache(cache_service, cache_key, region_id)
        LOGGER.info(
            "tenant_region_resolved tenant_id=%s region_id=%s source=db",
            tenant_id,
            region_id or DEFAULT_REGION_ID,
        )
        return region_id or DEFAULT_REGION_ID

    async def _read_cache(self, cache_service, cache_key: str) -> str | None:
        breaker = getattr(cache_service, "breaker", None)
        client = getattr(cache_service, "client", None)
        if breaker is None or client is None:
            return None
        try:
            value = await breaker.call(
                client.get,
                cache_key,
                fallback=lambda: on_redis_open(None),
            )
        except REDIS_FAILURES:
            return None
        return str(value) if value else None

    async def _write_cache(self, cache_service, cache_key: str, region_id: str) -> None:
        breaker = getattr(cache_service, "breaker", None)
        client = getattr(cache_service, "client", None)
        if breaker is None or client is None:
            return
        try:
            await breaker.call(
                client.set,
                cache_key,
                region_id,
                ex=REGION_CACHE_TTL_SECONDS,
                fallback=lambda: on_redis_open(None),
            )
        except REDIS_FAILURES:
            return
