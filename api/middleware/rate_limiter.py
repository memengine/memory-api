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
API_KEY_RATE_LIMIT_PREFIX = "api_key_rate"
DEFAULT_TENANT_RATE_LIMIT_PER_MINUTE = 120
TENANT_RATE_LIMIT_TTL_SECONDS = 120
REDIS_FAILURES = (RedisConnectionError, RedisTimeoutError)
RATE_LIMIT_INCREMENT_SCRIPT = """
local current = redis.call('INCR', KEYS[1])
if current == 1 then
    redis.call('EXPIRE', KEYS[1], ARGV[1])
end
return current
"""
API_KEY_SCOPED_PREFIXES = (
    "/v1/memories",
    "/v1/users",
    "/v1/api-keys",
    "/v1/agents",
    "/v1/tenant",
)


class RateLimiterMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: Callable[..., Any]) -> Response:
        auth_scheme = getattr(request.state, "auth_scheme", None)
        path = request.url.path
        if auth_scheme != "apikey" or not self._is_scoped_path(path):
            return await call_next(request)

        tenant_id = getattr(request.state, "tenant_id", None)
        api_key_id = getattr(request.state, "api_key_id", None)
        raw_permissions = getattr(request.state, "api_key_permissions", None)
        permissions = {
            str(item).strip().lower()
            for item in (raw_permissions or ())
            if str(item).strip()
        }
        required_permission = self._required_permission(request.method, path)
        if (
            raw_permissions is not None
            and required_permission
            and "admin" not in permissions
            and required_permission not in permissions
        ):
            return self._forbidden(request, required_permission)

        app = request.app
        if app is None:
            return await call_next(request)
        cache_service = getattr(app.state, "cache_service", None)

        if not tenant_id or not api_key_id or not hasattr(cache_service, "client"):
            return await call_next(request)

        window_minute = int(time.time()) // 60
        api_key_limit = max(
            1,
            int(getattr(request.state, "api_key_rate_limit_per_minute", 60) or 60),
        )
        breaker = getattr(cache_service, "breaker", None)
        try:
            api_key_count = await self._increment(
                cache_service,
                breaker,
                f"{API_KEY_RATE_LIMIT_PREFIX}:{api_key_id}:{window_minute}",
            )
        except REDIS_FAILURES:
            return await call_next(request)

        if api_key_count > api_key_limit:
            return self._rate_limited(request, scope="api_key")

        if request.method == "POST" and path == "/v1/memories/add":
            tenant_limit = int(
                os.getenv("TENANT_RATE_LIMIT_PER_MINUTE", str(DEFAULT_TENANT_RATE_LIMIT_PER_MINUTE))
            )
            try:
                tenant_count = await self._increment(
                    cache_service,
                    breaker,
                    f"{TENANT_RATE_LIMIT_PREFIX}:{tenant_id}:{window_minute}",
                )
            except REDIS_FAILURES:
                return await call_next(request)
            if tenant_count > tenant_limit:
                return self._rate_limited(request, scope="tenant")

        return await call_next(request)

    @staticmethod
    def _is_scoped_path(path: str) -> bool:
        return any(path == prefix or path.startswith(f"{prefix}/") for prefix in API_KEY_SCOPED_PREFIXES)

    @staticmethod
    def _required_permission(method: str, path: str) -> str | None:
        if path.startswith(("/v1/api-keys", "/v1/tenant")):
            return "admin"
        if path.startswith("/v1/agents"):
            return "read" if method == "GET" else "admin"
        if method == "DELETE":
            return "delete"
        if path == "/v1/memories/retrieve" or method == "GET":
            return "read"
        return "write"

    @staticmethod
    async def _increment(cache_service: Any, breaker: Any, cache_key: str) -> int:
        if breaker is not None:
            return int(
                await breaker.call(
                    cache_service.client.eval,
                    RATE_LIMIT_INCREMENT_SCRIPT,
                    1,
                    cache_key,
                    TENANT_RATE_LIMIT_TTL_SECONDS,
                    fallback=lambda: on_redis_open(0),
                )
            )
        return int(
            await cache_service.client.eval(
                RATE_LIMIT_INCREMENT_SCRIPT,
                1,
                cache_key,
                TENANT_RATE_LIMIT_TTL_SECONDS,
            )
        )

    @classmethod
    def _forbidden(cls, request: Request, required_permission: str) -> JSONResponse:
        return JSONResponse(
            status_code=403,
            content={
                "error": "insufficient_api_key_permission",
                "code": "AUTH_403",
                "request_id": cls._request_id(request),
                "details": {"required_permission": required_permission},
            },
        )

    @classmethod
    def _rate_limited(cls, request: Request, *, scope: str) -> JSONResponse:
        retry_after_seconds = cls._retry_after_seconds()
        return JSONResponse(
            status_code=429,
            content={
                "error": "rate_limited",
                "code": "RATE_429",
                "request_id": cls._request_id(request),
                "details": {
                    "scope": scope,
                    "retry_after_seconds": retry_after_seconds,
                },
            },
            headers={"Retry-After": str(retry_after_seconds)},
        )

    @staticmethod
    def _request_id(request: Request) -> str:
        state_request_id = getattr(request.state, "request_id", None)
        header_request_id = request.headers.get("x-request-id")
        return str(state_request_id or header_request_id or uuid.uuid4())

    @staticmethod
    def _retry_after_seconds() -> int:
        current_second = int(time.time()) % 60
        return 60 if current_second == 0 else 60 - current_second
