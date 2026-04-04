from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC
from datetime import datetime

import fakeredis.aioredis
from fastapi import FastAPI
from fastapi import Request
from fastapi.testclient import TestClient

from api.db.models import ApiKey
from api.middleware.auth import AuthMiddleware
from api.utils.crypto import api_key_prefix
from api.utils.crypto import hash_api_key


class FakeExecuteResult:
    def __init__(self, items) -> None:
        self._items = list(items)

    def scalars(self):
        return self

    def all(self):
        return list(self._items)


@dataclass
class FakeSession:
    api_keys: list[ApiKey]
    execute_calls: int = 0
    commit_calls: int = 0

    async def __aenter__(self) -> FakeSession:
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        return None

    async def execute(self, _query):
        self.execute_calls += 1
        return FakeExecuteResult([api_key for api_key in self.api_keys if api_key.is_active])

    async def commit(self) -> None:
        self.commit_calls += 1


class FakeSessionFactory:
    def __init__(self, api_keys: list[ApiKey]) -> None:
        self.session = FakeSession(api_keys=api_keys)

    def __call__(self) -> FakeSession:
        return self.session


def make_tenant_api_key(*, raw_key: str, is_active: bool = True) -> tuple[ApiKey, uuid.UUID]:
    tenant_id = uuid.uuid4()
    api_key = ApiKey(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        user_id=None,
        key_hash=hash_api_key(raw_key),
        key_prefix=api_key_prefix(raw_key),
        name="Tenant SDK key",
        permissions=["write"],
        rate_limit_per_minute=60,
        created_at=datetime.now(UTC),
        last_used_at=None,
        is_active=is_active,
    )
    return api_key, tenant_id


def build_test_app(*, session_factory: FakeSessionFactory, redis_client) -> FastAPI:
    app = FastAPI()
    app.add_middleware(
        AuthMiddleware,
        session_factory=session_factory,
        redis_client=redis_client,
        clerk_issuer="https://clerk.example.test",
        clerk_jwks_url="https://clerk.example.test/.well-known/jwks.json",
    )

    @app.get("/private")
    async def private(request: Request) -> dict[str, str | None]:
        return {
            "tenant_id": getattr(request.state, "tenant_id", None),
            "user_id": getattr(request.state, "user_id", None),
            "auth_scheme": getattr(request.state, "auth_scheme", None),
        }

    return app


def test_valid_tenant_api_key_sets_request_state_tenant_id() -> None:
    raw_api_key = "mem_live_valid_key"
    api_key, tenant_id = make_tenant_api_key(raw_key=raw_api_key)
    session_factory = FakeSessionFactory(api_keys=[api_key])
    redis_client = fakeredis.aioredis.FakeRedis(decode_responses=True)
    app = build_test_app(session_factory=session_factory, redis_client=redis_client)

    with TestClient(app) as client:
        response = client.get("/private", headers={"Authorization": f"ApiKey {raw_api_key}"})

    assert response.status_code == 200
    assert response.json() == {
        "tenant_id": str(tenant_id),
        "user_id": None,
        "auth_scheme": "apikey",
    }


def test_invalid_tenant_api_key_returns_401() -> None:
    api_key, _tenant_id = make_tenant_api_key(raw_key="mem_live_valid_key")
    session_factory = FakeSessionFactory(api_keys=[api_key])
    redis_client = fakeredis.aioredis.FakeRedis(decode_responses=True)
    app = build_test_app(session_factory=session_factory, redis_client=redis_client)

    with TestClient(app) as client:
        response = client.get("/private", headers={"Authorization": "ApiKey mem_live_wrong_key"})

    assert response.status_code == 401
    assert response.json()["code"] == "AUTH_001"


def test_revoked_tenant_api_key_returns_401() -> None:
    raw_api_key = "mem_live_revoked_key"
    api_key, _tenant_id = make_tenant_api_key(raw_key=raw_api_key, is_active=False)
    session_factory = FakeSessionFactory(api_keys=[api_key])
    redis_client = fakeredis.aioredis.FakeRedis(decode_responses=True)
    app = build_test_app(session_factory=session_factory, redis_client=redis_client)

    with TestClient(app) as client:
        response = client.get("/private", headers={"Authorization": f"ApiKey {raw_api_key}"})

    assert response.status_code == 401
    assert response.json()["code"] == "AUTH_001"
