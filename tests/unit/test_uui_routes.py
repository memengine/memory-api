from __future__ import annotations

from datetime import UTC
from datetime import datetime
from types import SimpleNamespace
import uuid

from fastapi import FastAPI
from fastapi.testclient import TestClient
from fastapi.responses import JSONResponse

from api.db.database import get_db_session
from api.dependencies import get_cache_service
from api.dependencies import get_qdrant_service
from api.errors import APIError
from api.routers.agents import router as agents_router
from api.routers.uui import router as uui_router
from api.services.global_agent_service import GlobalAgentService
from api.services.uui_service import UUIService


async def _override_db_session():
    yield SimpleNamespace()


def _build_test_app() -> FastAPI:
    app = FastAPI()
    
    @app.exception_handler(APIError)
    async def api_error_handler(_request, exc: APIError):
        return JSONResponse(
            status_code=exc.status_code,
            content={
                "error": exc.error,
                "code": exc.code,
                "details": exc.details,
                "request_id": "test-request-id",
            },
        )

    app.include_router(uui_router)
    app.include_router(agents_router)
    app.dependency_overrides[get_db_session] = _override_db_session
    app.dependency_overrides[get_cache_service] = lambda: SimpleNamespace()
    app.dependency_overrides[get_qdrant_service] = lambda: SimpleNamespace()
    return app


def test_register_uui_returns_token(monkeypatch) -> None:
    app = _build_test_app()
    now = datetime.now(UTC)
    created_user = SimpleNamespace(
        id=uuid.uuid4(),
        uui_token="uui_" + ("a" * 48),
        email="alex@example.com",
        display_name="Alex",
        created_at=now,
        is_active=True,
        memory_count=0,
    )

    async def fake_register(self, *, email=None, display_name=None):
        assert email == "alex@example.com"
        assert display_name == "Alex"
        return created_user

    monkeypatch.setattr(UUIService, "register", fake_register)

    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.post(
            "/v1/uui/register",
            json={"email": "alex@example.com", "display_name": "Alex"},
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["data"]["uui_token"].startswith("uui_")
    assert len(payload["data"]["uui_token"]) == 52
    assert payload["data"]["email"] == "alex@example.com"


def test_register_uui_duplicate_email_returns_409(monkeypatch) -> None:
    app = _build_test_app()

    async def fake_register(self, *, email=None, display_name=None):
        raise APIError(
            status_code=409,
            code="UUI_409",
            error="memory_passport_exists",
            details={
                "message": (
                    "A Memory Passport already exists for this email. "
                    "Use your saved token, or register without email if you are creating a separate test identity."
                )
            },
        )

    monkeypatch.setattr(UUIService, "register", fake_register)

    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.post(
            "/v1/uui/register",
            json={"email": "alex@example.com", "display_name": "Alex"},
        )

    assert response.status_code == 409
    payload = response.json()
    assert payload["code"] == "UUI_409"
    assert payload["error"] == "memory_passport_exists"
    assert "already exists" in payload["details"]["message"]


def test_get_global_agent_profile_returns_public_fields(monkeypatch) -> None:
    app = _build_test_app()
    agent_id = uuid.uuid4()

    async def fake_get_public_profile(self, resolved_agent_id: str):
        assert resolved_agent_id == str(agent_id)
        return SimpleNamespace(
            id=agent_id,
            name="Docs Agent",
            description="Helps with developer docs.",
            logo_url="https://cdn.example.com/logo.png",
            website_url="https://docs-agent.example.com",
            is_verified=True,
            default_categories_requested=["expertise", "preference"],
        )

    monkeypatch.setattr(GlobalAgentService, "get_public_profile", fake_get_public_profile)

    with TestClient(app) as client:
        response = client.get(f"/v1/agents/global/{agent_id}")

    assert response.status_code == 200
    payload = response.json()
    assert payload["data"]["name"] == "Docs Agent"
    assert payload["data"]["website_url"] == "https://docs-agent.example.com"
    assert payload["data"]["is_verified"] is True


def test_list_uui_grants_returns_memory_stats(monkeypatch) -> None:
    app = _build_test_app()
    user_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    resolved_user = SimpleNamespace(
        id=user_id,
        uui_token="uui_valid",
        email="alex@example.com",
        display_name="Alex",
        created_at=datetime.now(UTC),
        is_active=True,
        memory_count=12,
    )

    async def fake_resolve(self, token: str):
        return resolved_user if token == "uui_valid" else None

    async def fake_get_grants(self, user_uui_id: str):
        assert user_uui_id == str(user_id)
        return [
            SimpleNamespace(
                id=uuid.uuid4(),
                user_uui_id=user_id,
                agent_id=agent_id,
                global_agent=SimpleNamespace(name="Docs Agent", logo_url="https://logo.example.com"),
                categories_allowed=["expertise", "preference"],
                access_type="read_write",
                granted_at=datetime.now(UTC),
                expires_at=None,
                is_active=True,
                revoked_at=None,
            )
        ]

    monkeypatch.setattr(UUIService, "resolve", fake_resolve)
    monkeypatch.setattr(UUIService, "get_grants", fake_get_grants)

    with TestClient(app) as client:
        response = client.get(
            "/v1/uui/me/grants",
            headers={"X-MemoryOS-UUI": "uui_valid"},
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["data"]["memory_count"] == 12
    assert payload["data"]["display_name"] == "Alex"
    assert payload["data"]["grants"][0]["agent_name"] == "Docs Agent"


def test_create_uui_grant_supports_duration(monkeypatch) -> None:
    app = _build_test_app()
    user_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    resolved_user = SimpleNamespace(
        id=user_id,
        uui_token="uui_valid",
        email=None,
        display_name="Alex",
        created_at=datetime.now(UTC),
        is_active=True,
        memory_count=4,
    )
    captured: dict[str, object] = {}

    async def fake_resolve(self, token: str):
        return resolved_user if token == "uui_valid" else None

    async def fake_create_grant(
        self,
        user_uui_id: str,
        agent_id: str,
        categories: list[str],
        access_type: str,
        expires_at=None,
    ):
        captured["user_uui_id"] = user_uui_id
        captured["agent_id"] = agent_id
        captured["categories"] = categories
        captured["access_type"] = access_type
        captured["expires_at"] = expires_at
        return SimpleNamespace(
            id=uuid.uuid4(),
            user_uui_id=uuid.UUID(user_uui_id),
            agent_id=uuid.UUID(agent_id),
            global_agent=SimpleNamespace(name="Planner Agent", logo_url=None),
            categories_allowed=categories,
            access_type=access_type,
            granted_at=datetime.now(UTC),
            expires_at=expires_at,
            is_active=True,
            revoked_at=None,
        )

    monkeypatch.setattr(UUIService, "resolve", fake_resolve)
    monkeypatch.setattr(UUIService, "create_grant", fake_create_grant)

    with TestClient(app) as client:
        response = client.post(
            "/v1/uui/me/grants",
            headers={"X-MemoryOS-UUI": "uui_valid"},
            json={
                "agent_id": str(agent_id),
                "categories": ["goal", "procedure"],
                "duration_days": 30,
            },
        )

    assert response.status_code == 200
    assert captured["user_uui_id"] == str(user_id)
    assert captured["agent_id"] == str(agent_id)
    assert captured["categories"] == ["goal", "procedure"]
    assert captured["access_type"] == "read_write"
    assert captured["expires_at"] is not None
    assert response.json()["data"]["access_type"] == "read_write"


def test_revoke_uui_grant_returns_revoked(monkeypatch) -> None:
    app = _build_test_app()
    user_id = uuid.uuid4()
    grant_id = uuid.uuid4()
    resolved_user = SimpleNamespace(
        id=user_id,
        uui_token="uui_valid",
        email=None,
        display_name="Alex",
        created_at=datetime.now(UTC),
        is_active=True,
        memory_count=2,
    )

    async def fake_resolve(self, token: str):
        return resolved_user if token == "uui_valid" else None

    async def fake_revoke_grant(self, user_uui_id: str, revoke_grant_id: str):
        return user_uui_id == str(user_id) and revoke_grant_id == str(grant_id)

    monkeypatch.setattr(UUIService, "resolve", fake_resolve)
    async def fake_revoke_grant_kw(self, user_uui_id: str, grant_id: str):
        return await fake_revoke_grant(self, user_uui_id, grant_id)

    monkeypatch.setattr(UUIService, "revoke_grant", fake_revoke_grant_kw)

    with TestClient(app) as client:
        response = client.delete(
            f"/v1/uui/me/grants/{grant_id}",
            headers={"X-MemoryOS-UUI": "uui_valid"},
        )

    assert response.status_code == 200
    assert response.json()["data"]["revoked"] is True


def test_delete_uui_data_returns_deleted_count(monkeypatch) -> None:
    app = _build_test_app()
    user_id = uuid.uuid4()
    resolved_user = SimpleNamespace(
        id=user_id,
        uui_token="uui_valid",
        email="alex@example.com",
        display_name="Alex",
        created_at=datetime.now(UTC),
        is_active=True,
        memory_count=7,
    )

    async def fake_resolve(self, token: str):
        return resolved_user if token == "uui_valid" else None

    async def fake_delete_user_data(self, *, uui_token: str):
        assert uui_token == "uui_valid"
        return True, 7

    monkeypatch.setattr(UUIService, "resolve", fake_resolve)
    monkeypatch.setattr(UUIService, "delete_user_data", fake_delete_user_data)

    with TestClient(app) as client:
        response = client.delete(
            "/v1/uui/me",
            headers={"X-MemoryOS-UUI": "uui_valid"},
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["data"]["deleted"] is True
    assert payload["data"]["memories_removed"] == 7
