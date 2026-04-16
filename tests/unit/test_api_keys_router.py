from __future__ import annotations

import uuid
from datetime import UTC
from datetime import datetime

from fastapi import FastAPI
from fastapi import Request
from fastapi.testclient import TestClient

from api.dependencies import get_api_key_service
from api.db.models import ApiKey
from api.routers.api_keys import router
from api.services.api_key_service import ApiKeyService
from api.utils.crypto import api_key_prefix
from api.utils.crypto import hash_api_key


class FakeScalarResult:
    def __init__(self, rows) -> None:
        self._rows = list(rows)

    def all(self):
        return list(self._rows)


class FakeExecuteResult:
    def __init__(self, rows) -> None:
        self._rows = list(rows)

    def scalars(self):
        return FakeScalarResult(self._rows)


class FakeSession:
    def __init__(self, api_keys: list[ApiKey]) -> None:
        self.api_keys = list(api_keys)
        self.commits = 0

    async def execute(self, query):
        tenant_id = None
        query_text = str(query).lower()
        active_only = "api_keys.is_active" in query_text and "true" in query_text
        for criterion in getattr(query, "_where_criteria", ()):
            right = getattr(criterion, "right", None)
            value = getattr(right, "value", None)
            if isinstance(value, uuid.UUID):
                tenant_id = value
                break

        rows = [
            api_key
            for api_key in self.api_keys
            if (tenant_id is None or api_key.tenant_id == tenant_id)
            and (not active_only or api_key.is_active)
        ]
        rows.sort(key=lambda item: (item.created_at or datetime.min.replace(tzinfo=UTC), item.id), reverse=True)
        return FakeExecuteResult(rows)

    async def commit(self) -> None:
        self.commits += 1

    async def refresh(self, _instance) -> None:
        return None

    async def get(self, model, key):
        if model is not ApiKey:
            return None
        for api_key in self.api_keys:
            if api_key.id == key:
                return api_key
        return None

    def add(self, instance) -> None:
        self.api_keys.append(instance)


def make_api_key(
    *,
    tenant_id: uuid.UUID,
    name: str,
    is_active: bool = True,
    created_at: datetime | None = None,
) -> ApiKey:
    raw_key = f"mem_{name.lower()}_{uuid.uuid4().hex[:8]}"
    return ApiKey(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        user_id=None,
        key_hash=hash_api_key(raw_key),
        key_prefix=api_key_prefix(raw_key),
        name=name,
        permissions=["read", "write"],
        rate_limit_per_minute=60,
        created_at=created_at or datetime.now(UTC),
        last_used_at=None,
        is_active=is_active,
    )


def build_test_app(session: FakeSession, tenant_id: str) -> FastAPI:
    app = FastAPI()

    @app.middleware("http")
    async def attach_auth_state(request: Request, call_next):
        request.state.tenant_id = tenant_id
        request.state.user_id = "user_3Bw5VPfkwkjsnda7XEojScqAEqx"
        request.state.auth_method = "clerk_jwt"
        request.state.auth_scheme = "bearer"
        return await call_next(request)

    service = ApiKeyService(session=session)
    app.dependency_overrides[get_api_key_service] = lambda: service
    app.include_router(router)
    return app


def test_list_api_keys_uses_tenant_scope_and_ignores_stale_user_id() -> None:
    tenant_id = uuid.uuid4()
    other_tenant_id = uuid.uuid4()
    session = FakeSession(
        [
            make_api_key(tenant_id=tenant_id, name="Tenant Key A"),
            make_api_key(tenant_id=tenant_id, name="Tenant Key B"),
            make_api_key(tenant_id=tenant_id, name="Revoked Key", is_active=False),
            make_api_key(tenant_id=other_tenant_id, name="Other Tenant Key"),
        ]
    )
    app = build_test_app(session, str(tenant_id))

    with TestClient(app) as client:
        response = client.get("/v1/api-keys")

    assert response.status_code == 200
    payload = response.json()
    assert [item["name"] for item in payload["data"]] == ["Tenant Key B", "Tenant Key A"]
    assert all(item["is_active"] is True for item in payload["data"])


def test_create_api_key_sets_tenant_id_and_not_user_id() -> None:
    tenant_id = uuid.uuid4()
    session = FakeSession([])
    app = build_test_app(session, str(tenant_id))

    with TestClient(app) as client:
        response = client.post(
            "/v1/api-keys",
            json={
                "name": "Dashboard Key",
                "permissions": ["read", "write"],
                "rate_limit_per_minute": 120,
            },
        )

    assert response.status_code == 200
    created = session.api_keys[0]
    assert created.tenant_id == tenant_id
    assert created.user_id is None
    assert response.json()["data"]["name"] == "Dashboard Key"
    assert response.json()["data"]["raw_key"].startswith("mem_")


def test_revoke_api_key_only_allows_current_tenant_key() -> None:
    tenant_id = uuid.uuid4()
    other_tenant_id = uuid.uuid4()
    own_key = make_api_key(tenant_id=tenant_id, name="Own Key")
    other_key = make_api_key(tenant_id=other_tenant_id, name="Other Key")
    session = FakeSession([own_key, other_key])
    app = build_test_app(session, str(tenant_id))

    with TestClient(app) as client:
        own_response = client.delete(f"/v1/api-keys/{own_key.id}")
        other_response = client.delete(f"/v1/api-keys/{other_key.id}")

    assert own_response.status_code == 200
    assert own_response.json()["data"]["deleted"] is True
    assert own_key.is_active is False

    assert other_response.status_code == 200
    assert other_response.json()["data"]["deleted"] is False
    assert other_key.is_active is True
