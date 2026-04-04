from __future__ import annotations

import os
import time
import uuid
from typing import Any
from typing import Callable

from redis.exceptions import ConnectionError as RedisConnectionError
from redis.exceptions import TimeoutError as RedisTimeoutError
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.responses import Response

from api.infra.fallbacks import on_redis_open

TENANT_RATE_LIMIT_PREFIX = "tenant_rate"
DEFAULT_TENANT_RATE_LIMIT_PER_MINUTE = 120
TENANT_RATE_LIMIT_TTL_SECONDS = 120
REDIS_FAILURES = (RedisConnectionError, RedisTimeoutError)


class RateLimiterMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: Callable[..., Any]) -> Response:
        if request.method != "POST" or request.url.path != "/v1/memories/add":
            return await call_next(request)

        tenant_id = getattr(request.state, "tenant_id", None)
        auth_scheme = getattr(request.state, "auth_scheme", None)
        cache_service = getattr(request.app.state, "cache_service", None)

        if not tenant_id or auth_scheme != "apikey" or not hasattr(cache_service, "client"):
            return await call_next(request)

        window_minute = int(time.time()) // 60
        rate_limit = int(
            os.getenv("TENANT_RATE_LIMIT_PER_MINUTE", str(DEFAULT_TENANT_RATE_LIMIT_PER_MINUTE))
        )
        cache_key = f"{TENANT_RATE_LIMIT_PREFIX}:{tenant_id}:{window_minute}"
        breaker = getattr(cache_service, "breaker", None)

        try:
            if breaker is not None:
                current_count = int(
                    await breaker.call(
                        cache_service.client.incr,
                        cache_key,
                        fallback=lambda: on_redis_open(0),
                    )
                )
                ttl = await breaker.call(
                    cache_service.client.ttl,
                    cache_key,
                    fallback=lambda: on_redis_open(-1),
                )
            else:
                current_count = int(await cache_service.client.incr(cache_key))
                ttl = await cache_service.client.ttl(cache_key)
            if ttl is None or ttl < 0:
                if breaker is not None:
                    await breaker.call(
                        cache_service.client.expire,
                        cache_key,
                        TENANT_RATE_LIMIT_TTL_SECONDS,
                        fallback=lambda: on_redis_open(None),
                    )
                else:
                    await cache_service.client.expire(
                        cache_key,
                        TENANT_RATE_LIMIT_TTL_SECONDS,
                    )
        except REDIS_FAILURES:
            force_open = getattr(breaker, "force_open", None)
            if callable(force_open):
                try:
                    force_open()
                except Exception:
                    pass
            return await call_next(request)

        if current_count > rate_limit:
            retry_after_seconds = self._retry_after_seconds()
            request_id = self._request_id(request)
            return JSONResponse(
                status_code=429,
                content={
                    "error": "rate_limited",
                    "code": "RATE_429",
                    "request_id": request_id,
                    "details": {
                        "scope": "tenant",
                        "retry_after_seconds": retry_after_seconds,
                    },
                },
                headers={"Retry-After": str(retry_after_seconds)},
            )

        return await call_next(request)

    @staticmethod
    def _request_id(request: Request) -> str:
        state_request_id = getattr(request.state, "request_id", None)
        header_request_id = request.headers.get("x-request-id")
        return str(state_request_id or header_request_id or uuid.uuid4())

    @staticmethod
    def _retry_after_seconds() -> int:
        current_second = int(time.time()) % 60
        return 60 if current_second == 0 else 60 - current_second
