from __future__ import annotations

import json
import os
import uuid
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.main import create_app
from api.db.models import ApiKey
from api.middleware.admin_auth import ADMIN_HEADER_NAME
from api.middleware.admin_auth import AdminAuthMiddleware
from api.utils.crypto import api_key_prefix
from api.utils.crypto import hash_api_key


VALID_ADMIN_SECRET = os.environ["ADMIN_SECRET"]


def build_test_app(*, admin_secret: str | None = VALID_ADMIN_SECRET) -> FastAPI:
    app = FastAPI()
    app.add_middleware(AdminAuthMiddleware, admin_secret=admin_secret)

    @app.get("/public")
    async def public() -> dict[str, bool]:
        return {"ok": True}

    @app.get("/v1/internal/ping")
    async def internal_ping() -> dict[str, bool]:
        return {"ok": True}

    return app


def test_internal_endpoint_requires_valid_admin_secret() -> None:
    app = build_test_app()

    with TestClient(app) as client:
        response = client.get("/v1/internal/ping")

    assert response.status_code == 403
    assert response.json() == {"error": "forbidden", "code": "ADMIN_AUTH_FAILED"}


def test_internal_endpoint_accepts_matching_admin_secret() -> None:
    app = build_test_app()

    with TestClient(app) as client:
        response = client.get(
            "/v1/internal/ping",
            headers={ADMIN_HEADER_NAME: VALID_ADMIN_SECRET},
        )

    assert response.status_code == 200
    assert response.json() == {"ok": True}


def test_non_internal_routes_are_not_affected_by_admin_auth() -> None:
    app = build_test_app()

    with TestClient(app) as client:
        response = client.get("/public")

    assert response.status_code == 200
    assert response.json() == {"ok": True}


def test_short_admin_secret_refuses_startup() -> None:
    app = build_test_app(admin_secret="too-short")

    with pytest.raises(RuntimeError, match="ADMIN_SECRET must be at least 32 characters long."):
        with TestClient(app):
            pass


def test_admin_access_is_logged_for_success_and_failure() -> None:
    app = build_test_app()

    with patch("api.middleware.admin_auth.LOGGER.info") as mock_info:
        with TestClient(app) as client:
            client.get("/v1/internal/ping")
            client.get(
                "/v1/internal/ping",
                headers={ADMIN_HEADER_NAME: VALID_ADMIN_SECRET},
            )

    assert mock_info.call_count == 2
    first_payload = json.loads(mock_info.call_args_list[0].args[0])
    second_payload = json.loads(mock_info.call_args_list[1].args[0])
    assert first_payload["success"] is False
    assert second_payload["success"] is True
    assert first_payload["event"] == "admin_endpoint_access"
    assert second_payload["event"] == "admin_endpoint_access"
    assert first_payload["endpoint"] == "/v1/internal/ping"
    assert second_payload["endpoint"] == "/v1/internal/ping"
    assert first_payload["method"] == "GET"
    assert second_payload["method"] == "GET"


def test_main_registers_admin_auth_middleware() -> None:
    app = create_app()

    assert any(middleware.cls is AdminAuthMiddleware for middleware in app.user_middleware)


def test_internal_route_skips_tenant_auth(monkeypatch: pytest.MonkeyPatch) -> None:
    app = create_app()

    with TestClient(app) as client:
        response = client.get(
            "/v1/internal/circuit-health",
            headers={ADMIN_HEADER_NAME: VALID_ADMIN_SECRET},
        )

    assert response.status_code == 200


def test_internal_route_still_requires_admin_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    raw_api_key = "mem_live_valid_key"
    tenant_id = uuid.uuid4()
    api_key = ApiKey(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        user_id=None,
        key_hash=hash_api_key(raw_api_key),
        key_prefix=api_key_prefix(raw_api_key),
        name="Tenant SDK key",
        permissions=["write"],
        rate_limit_per_minute=60,
        is_active=True,
    )

    class FakeExecuteResult:
        def __init__(self, items) -> None:
            self._items = list(items)

        def scalars(self):
            return self

        def all(self):
            return list(self._items)

    class FakeSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb) -> None:
            return None

        async def execute(self, _query):
            return FakeExecuteResult([api_key])

        async def commit(self) -> None:
            return None

    monkeypatch.setattr("api.middleware.auth.SessionLocal", lambda: FakeSession())
    app = create_app()

    with TestClient(app) as client:
        response = client.get(
            "/v1/internal/circuit-health",
            headers={"Authorization": f"ApiKey {raw_api_key}"},
        )

    assert response.status_code == 403
    assert response.json() == {"error": "forbidden", "code": "ADMIN_AUTH_FAILED"}
