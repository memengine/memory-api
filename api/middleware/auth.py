from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import uuid
from dataclasses import dataclass
from datetime import UTC
from datetime import datetime
from typing import Any
from typing import Callable

import httpx
import redis.asyncio as redis
from redis.asyncio.retry import Retry
from jose import jwk
from jose import jwt
from jose.exceptions import JWTError
from jose.utils import base64url_decode
from redis.asyncio import Redis
from redis.backoff import NoBackoff
from redis.exceptions import ConnectionError as RedisConnectionError
from redis.exceptions import TimeoutError as RedisTimeoutError
from sqlalchemy import select
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.responses import Response

from api.db.database import SessionLocal
from api.db.cache import get_redis_url
from api.db.models import ApiKey
from api.infra.circuit_breaker_registry import CircuitBreakerRegistry
from api.infra.fallbacks import on_redis_open
from api.utils.crypto import api_key_prefix
from api.utils.crypto import fingerprint_api_key
from api.utils.crypto import verify_api_key


LOGGER = logging.getLogger("memoryos.auth")
AUTH_CACHE_TTL_SECONDS = 300
JWKS_CACHE_TTL_SECONDS = 300
AUTH_CACHE_PREFIX = "apikey"
AUTH_FAILURE_PREFIX = "memoryos:auth:failure"
REDIS_FAILURES = (RedisConnectionError, RedisTimeoutError)


@dataclass(slots=True)
class ApiKeyAuthResult:
    tenant_id: str | None
    user_id: str | None
    api_key_id: str | None
    key_hash: str


class _DirectRedisBreaker:
    async def call(self, fn, *args, fallback=None, **kwargs):
        return await fn(*args, **kwargs)


class AuthMiddleware(BaseHTTPMiddleware):
    def __init__(
        self,
        app,
        *,
        session_factory: Callable[[], Any] | None = None,
        redis_client: Redis | None = None,
        redis_url: str | None = None,
        http_client: httpx.AsyncClient | None = None,
        clerk_issuer: str | None = None,
        clerk_jwks_url: str | None = None,
    ) -> None:
        super().__init__(app)
        self.session_factory = session_factory or SessionLocal
        self.redis_client = redis_client or redis.from_url(
            get_redis_url(redis_url),
            encoding="utf-8",
            decode_responses=True,
            socket_connect_timeout=0.1,
            socket_timeout=0.1,
            retry=Retry(NoBackoff(), 0),
            retry_on_timeout=False,
        )
        self.redis_breaker = (
            _DirectRedisBreaker()
            if redis_client is not None
            else CircuitBreakerRegistry.get_instance().redis_cb
        )
        self.http_client = http_client or httpx.AsyncClient()
        self.clerk_issuer = (clerk_issuer or os.getenv("CLERK_JWT_ISSUER", "")).rstrip("/")
        self.clerk_jwks_url = clerk_jwks_url or os.getenv(
            "CLERK_JWKS_URL",
            f"{self.clerk_issuer}/.well-known/jwks.json" if self.clerk_issuer else "",
        )
        self._jwks_cache: dict[str, Any] | None = None
        self._jwks_cached_at = 0.0

    def _mark_redis_unavailable(self) -> None:
        force_open = getattr(self.redis_breaker, "force_open", None)
        if callable(force_open):
            try:
                force_open()
            except Exception:
                return None

    async def dispatch(self, request: Request, call_next: Callable[..., Any]) -> Response:
        # /v1/internal/* is protected by AdminAuthMiddleware — skip tenant auth
        if request.url.path.startswith("/v1/internal/"):
            return await call_next(request)

        if self._is_public_endpoint(request):
            return await call_next(request)

        request_id = self._request_id(request)
        auth_header = request.headers.get("authorization", "").strip()
        if not auth_header:
            return await self._unauthorized(
                request=request,
                request_id=request_id,
                reason="Missing Authorization header",
            )

        try:
            scheme, credentials = auth_header.split(" ", 1)
        except ValueError:
            return await self._unauthorized(
                request=request,
                request_id=request_id,
                reason="Malformed Authorization header",
            )

        scheme_normalized = scheme.lower()
        if scheme_normalized == "bearer":
            user_id = await self._authenticate_jwt(credentials.strip())
            if user_id is None:
                return await self._unauthorized(
                    request=request,
                    request_id=request_id,
                    reason="JWT verification failed",
                )
            request.state.user_id = user_id
            request.state.auth_scheme = "bearer"
            return await call_next(request)

        if scheme_normalized == "apikey":
            auth_result = await self._authenticate_api_key(credentials.strip())
            if auth_result is None:
                return await self._unauthorized(
                    request=request,
                    request_id=request_id,
                    reason="API key verification failed",
                )
            request.state.tenant_id = auth_result.tenant_id
            request.state.user_id = auth_result.user_id
            request.state.api_key_id = auth_result.api_key_id
            request.state.auth_scheme = "apikey"
            return await call_next(request)

        return await self._unauthorized(
            request=request,
            request_id=request_id,
            reason="Unsupported authorization scheme",
        )

    async def _authenticate_jwt(self, token: str) -> str | None:
        if not self.clerk_jwks_url:
            return None

        try:
            header = jwt.get_unverified_header(token)
            jwks = await self._get_jwks()
            key_data = next(
                (
                    key
                    for key in jwks.get("keys", [])
                    if key.get("kid") == header.get("kid")
                ),
                None,
            )
            if key_data is None:
                return None

            public_key = jwk.construct(key_data)
            message, encoded_signature = token.rsplit(".", 1)
            decoded_signature = base64url_decode(encoded_signature.encode("utf-8"))
            if not public_key.verify(message.encode("utf-8"), decoded_signature):
                return None

            claims = jwt.get_unverified_claims(token)
            if self.clerk_issuer and claims.get("iss") not in {None, self.clerk_issuer}:
                return None

            expires_at = claims.get("exp")
            if expires_at is not None and time.time() >= float(expires_at):
                return None

            subject = claims.get("sub")
            return str(subject) if subject else None
        except (JWTError, ValueError, TypeError, httpx.HTTPError):
            return None

    async def _authenticate_api_key(self, raw_api_key: str) -> ApiKeyAuthResult | None:
        cache_key = self._api_key_cache_key(raw_api_key)
        try:
            cached_auth = await asyncio.wait_for(
                self._get_cached_api_key_auth(cache_key, raw_api_key),
                timeout=0.2,
            )
        except TimeoutError:
            self._mark_redis_unavailable()
            cached_auth = None
        if cached_auth is not None:
            return cached_auth

        async with self.session_factory() as session:
            result = await session.execute(
                select(ApiKey).where(
                    ApiKey.is_active.is_(True),
                    ApiKey.key_prefix == api_key_prefix(raw_api_key),
                )
            )
            api_keys = list(result.scalars().all())
            if not api_keys:
                result = await session.execute(
                    select(ApiKey).where(ApiKey.is_active.is_(True))
                )
                api_keys = list(result.scalars().all())
            for api_key in api_keys:
                if verify_api_key(raw_api_key, api_key.key_hash):
                    api_key.last_used_at = datetime.now(UTC)
                    if hasattr(session, "commit"):
                        await session.commit()

                    auth_result = ApiKeyAuthResult(
                        tenant_id=str(api_key.tenant_id) if api_key.tenant_id else None,
                        user_id=str(api_key.user_id) if api_key.user_id else None,
                        api_key_id=str(api_key.id),
                        key_hash=api_key.key_hash,
                    )
                    try:
                        await asyncio.wait_for(
                            self._cache_api_key_auth(cache_key, auth_result),
                            timeout=0.2,
                        )
                    except TimeoutError:
                        self._mark_redis_unavailable()
                        pass
                    return auth_result
        return None

    async def _get_jwks(self) -> dict[str, Any]:
        if self._jwks_cache and (time.time() - self._jwks_cached_at) < JWKS_CACHE_TTL_SECONDS:
            return self._jwks_cache

        response = await self.http_client.get(self.clerk_jwks_url, timeout=5.0)
        response.raise_for_status()
        payload = response.json()
        self._jwks_cache = payload
        self._jwks_cached_at = time.time()
        return payload

    async def _get_cached_api_key_auth(
        self,
        cache_key: str,
        raw_api_key: str,
    ) -> ApiKeyAuthResult | None:
        try:
            cached_value = await self.redis_breaker.call(
                self.redis_client.get,
                cache_key,
                fallback=lambda: on_redis_open(None),
            )
        except REDIS_FAILURES:
            self._mark_redis_unavailable()
            return None

        if cached_value is None:
            return None

        try:
            payload = json.loads(cached_value)
        except json.JSONDecodeError:
            return None

        key_hash = str(payload.get("key_hash", ""))
        if not key_hash or not verify_api_key(raw_api_key, key_hash):
            return None

        tenant_id = payload.get("tenant_id")
        user_id = payload.get("user_id")
        if not tenant_id and not user_id:
            return None

        return ApiKeyAuthResult(
            tenant_id=str(tenant_id) if tenant_id else None,
            user_id=str(user_id) if user_id else None,
            api_key_id=str(payload.get("api_key_id")) if payload.get("api_key_id") else None,
            key_hash=key_hash,
        )

    async def _cache_api_key_auth(self, cache_key: str, auth_result: ApiKeyAuthResult) -> None:
        try:
            await self.redis_breaker.call(
                self.redis_client.set,
                cache_key,
                json.dumps(
                    {
                        "tenant_id": auth_result.tenant_id,
                        "user_id": auth_result.user_id,
                        "api_key_id": auth_result.api_key_id,
                        "key_hash": auth_result.key_hash,
                    }
                ),
                ex=AUTH_CACHE_TTL_SECONDS,
                fallback=lambda: on_redis_open(None),
            )
        except REDIS_FAILURES:
            self._mark_redis_unavailable()
            return None

    async def _unauthorized(
        self,
        *,
        request: Request,
        request_id: str,
        reason: str,
    ) -> JSONResponse:
        attempt_count = await self._record_auth_failure(request)
        LOGGER.warning(
            json.dumps(
                {
                    "event": "auth_failure",
                    "code": "AUTH_001",
                    "reason": reason,
                    "path": request.url.path,
                    "method": request.method,
                    "ip_address": self._client_ip(request),
                    "attempt_count": attempt_count,
                    "request_id": request_id,
                }
            )
        )
        return JSONResponse(
            status_code=401,
            content={
                "error": "unauthorized",
                "code": "AUTH_001",
                "request_id": request_id,
            },
        )

    async def _record_auth_failure(self, request: Request) -> int:
        try:
            key = self._failure_cache_key(self._client_ip(request))
            attempt_count = int(
                await self.redis_breaker.call(
                    self.redis_client.incr,
                    key,
                    fallback=lambda: on_redis_open(1),
                )
            )
            ttl = await self.redis_breaker.call(
                self.redis_client.ttl,
                key,
                fallback=lambda: on_redis_open(-1),
            )
            if ttl is None or ttl < 0:
                await self.redis_breaker.call(
                    self.redis_client.expire,
                    key,
                    AUTH_CACHE_TTL_SECONDS,
                    fallback=lambda: on_redis_open(None),
                )
            return attempt_count
        except REDIS_FAILURES:
            self._mark_redis_unavailable()
            return 1

    @staticmethod
    def _request_id(request: Request) -> str:
        state_request_id = getattr(request.state, "request_id", None)
        header_request_id = request.headers.get("x-request-id")
        return str(state_request_id or header_request_id or uuid.uuid4())

    @staticmethod
    def _client_ip(request: Request) -> str:
        if request.client is None or request.client.host is None:
            return "unknown"
        return request.client.host

    @staticmethod
    def _is_public_endpoint(request: Request) -> bool:
        path = request.url.path
        if request.method == "GET" and path in {"/health", "/docs", "/redoc", "/openapi.json"}:
            return True
        if request.method == "POST" and path.startswith("/v1/webhooks/"):
            return True
        return False

    @staticmethod
    def _api_key_cache_key(raw_api_key: str) -> str:
        return f"{AUTH_CACHE_PREFIX}:{fingerprint_api_key(raw_api_key)[:16]}:tenant_auth"

    @staticmethod
    def _failure_cache_key(ip_address: str) -> str:
        return f"{AUTH_FAILURE_PREFIX}:{ip_address}"
