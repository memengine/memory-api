from __future__ import annotations

from types import SimpleNamespace

import pytest
from starlette.requests import Request
from starlette.responses import Response

from api.middleware.rate_limiter import RateLimiterMiddleware


class _Redis:
    def __init__(self) -> None:
        self.counts: dict[str, int] = {}
        self.expirations: dict[str, int] = {}

    async def eval(self, _script: str, key_count: int, key: str, ttl: int) -> int:
        assert key_count == 1
        self.counts[key] = self.counts.get(key, 0) + 1
        if self.counts[key] == 1:
            self.expirations[key] = ttl
        return self.counts[key]


def test_api_key_policy_matches_route_segments_not_nearby_prefixes() -> None:
    assert RateLimiterMiddleware._is_scoped_path("/v1/memories") is True
    assert RateLimiterMiddleware._is_scoped_path("/v1/memories/retrieve") is True
    assert RateLimiterMiddleware._is_scoped_path("/v1/memories-public") is False


def _request(
    *,
    method: str,
    path: str,
    permissions: tuple[str, ...],
    api_key_id: str = "key-1",
    limit: int = 60,
    redis: _Redis | None = None,
) -> Request:
    cache_service = SimpleNamespace(client=redis or _Redis())
    request = Request(
        {
            "type": "http",
            "http_version": "1.1",
            "method": method,
            "scheme": "https",
            "path": path,
            "raw_path": path.encode(),
            "query_string": b"",
            "headers": [],
            "client": ("127.0.0.1", 1),
            "server": ("test", 443),
            "app": SimpleNamespace(state=SimpleNamespace(cache_service=cache_service)),
        }
    )
    request.state.auth_scheme = "apikey"
    request.state.tenant_id = "tenant-1"
    request.state.api_key_id = api_key_id
    request.state.api_key_permissions = permissions
    request.state.api_key_rate_limit_per_minute = limit
    return request


@pytest.mark.asyncio
async def test_write_route_rejects_read_only_api_key() -> None:
    middleware = RateLimiterMiddleware(lambda *_args, **_kwargs: None)
    called = False

    async def call_next(_request: Request) -> Response:
        nonlocal called
        called = True
        return Response(status_code=204)

    response = await middleware.dispatch(
        _request(method="POST", path="/v1/memories/add", permissions=("read",)),
        call_next,
    )

    assert response.status_code == 403
    assert not called


@pytest.mark.asyncio
async def test_retrieve_route_accepts_read_permission() -> None:
    middleware = RateLimiterMiddleware(lambda *_args, **_kwargs: None)

    async def call_next(_request: Request) -> Response:
        return Response(status_code=204)

    response = await middleware.dispatch(
        _request(method="POST", path="/v1/memories/retrieve", permissions=("read",)),
        call_next,
    )

    assert response.status_code == 204


@pytest.mark.asyncio
async def test_api_key_rate_limit_is_enforced_per_key() -> None:
    redis = _Redis()
    middleware = RateLimiterMiddleware(lambda *_args, **_kwargs: None)

    async def call_next(_request: Request) -> Response:
        return Response(status_code=204)

    first = await middleware.dispatch(
        _request(
            method="POST",
            path="/v1/memories/retrieve",
            permissions=("read",),
            limit=1,
            redis=redis,
        ),
        call_next,
    )
    second = await middleware.dispatch(
        _request(
            method="POST",
            path="/v1/memories/retrieve",
            permissions=("read",),
            limit=1,
            redis=redis,
        ),
        call_next,
    )
    other_key = await middleware.dispatch(
        _request(
            method="POST",
            path="/v1/memories/retrieve",
            permissions=("read",),
            api_key_id="key-2",
            limit=1,
            redis=redis,
        ),
        call_next,
    )

    assert first.status_code == 204
    assert second.status_code == 429
    assert other_key.status_code == 204
    assert all(ttl == 120 for ttl in redis.expirations.values())
