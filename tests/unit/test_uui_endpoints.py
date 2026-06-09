from __future__ import annotations

from datetime import UTC
from datetime import datetime
from types import SimpleNamespace
import uuid

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from api.db.database import get_db_session
from api.dependencies import get_cache_service
from api.dependencies import get_qdrant_service
from api.errors import APIError
from api.routers.uui import _current_universal_user
from api.routers.uui import router as uui_router
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
    app.dependency_overrides[get_db_session] = _override_db_session
    app.dependency_overrides[get_cache_service] = lambda: SimpleNamespace()
    app.dependency_overrides[get_qdrant_service] = lambda: SimpleNamespace()
    return app


def test_send_otp_endpoint_returns_sent(monkeypatch) -> None:
    app = _build_test_app()

    async def fake_is_rate_limited(self, email: str):
        return False

    async def fake_send_otp(self, email: str):
        return email == "alex@example.com"

    monkeypatch.setattr(UUIService, "is_otp_rate_limited", fake_is_rate_limited)
    monkeypatch.setattr(UUIService, "send_otp", fake_send_otp)

    with TestClient(app) as client:
        response = client.post("/v1/uui/otp/send", json={"email": "alex@example.com"})

    assert response.status_code == 200
    assert response.json()["data"]["sent"] is True
    assert response.json()["data"]["reason"] is None


def test_send_otp_endpoint_rate_limited(monkeypatch) -> None:
    app = _build_test_app()

    async def fake_is_rate_limited(self, email: str):
        return True

    monkeypatch.setattr(UUIService, "is_otp_rate_limited", fake_is_rate_limited)

    with TestClient(app) as client:
        response = client.post("/v1/uui/otp/send", json={"email": "alex@example.com"})

    assert response.status_code == 200
    assert response.json()["data"]["sent"] is False
    assert response.json()["data"]["reason"] == "rate_limited"


def test_verify_otp_endpoint_returns_session_token(monkeypatch) -> None:
    app = _build_test_app()
    resolved_user = SimpleNamespace(
        id=uuid.uuid4(),
        uui_token="uui_" + ("a" * 48),
        email="alex@example.com",
        display_name="Alex",
        created_at=datetime.now(UTC),
        is_active=True,
        memory_count=0,
    )

    async def fake_verify_otp(self, email: str, otp: str):
        return resolved_user if otp == "123456" else None

    monkeypatch.setattr(UUIService, "verify_otp", fake_verify_otp)
    monkeypatch.setenv("UUI_SESSION_SECRET", "test-session-secret")

    with TestClient(app) as client:
        response = client.post(
            "/v1/uui/otp/verify",
            json={"email": "alex@example.com", "otp": "123456"},
        )

    assert response.status_code == 200
    payload = response.json()["data"]
    assert payload["email"] == "alex@example.com"
    assert isinstance(payload["session_token"], str)
    assert payload["session_token"]


def test_verify_otp_endpoint_rejects_invalid_otp(monkeypatch) -> None:
    app = _build_test_app()

    async def fake_verify_otp(self, email: str, otp: str):
        return None

    monkeypatch.setattr(UUIService, "verify_otp", fake_verify_otp)

    with TestClient(app) as client:
        response = client.post(
            "/v1/uui/otp/verify",
            json={"email": "alex@example.com", "otp": "000000"},
        )

    assert response.status_code == 401
    assert response.json()["error"] == "invalid_otp"


def test_get_me_returns_profile_and_grants(monkeypatch) -> None:
    app = _build_test_app()
    user_id = uuid.uuid4()
    now = datetime.now(UTC)
    resolved_user = SimpleNamespace(
        id=user_id,
        uui_token="uui_" + ("a" * 48),
        email="alex@example.com",
        display_name="Alex",
        created_at=now,
        is_active=True,
        memory_count=2,
    )

    async def override_current_user():
        return resolved_user

    async def fake_get_grants(self, user_uui_id: str):
        return [
            SimpleNamespace(
                id=uuid.uuid4(),
                user_uui_id=user_id,
                agent_id=uuid.uuid4(),
                global_agent=SimpleNamespace(
                    name="Docs Agent",
                    logo_url="https://cdn.example.com/logo.png",
                    website_url="https://docs.example.com",
                    is_verified=True,
                ),
                categories_allowed=["expertise"],
                access_type="read_only",
                granted_at=now,
                expires_at=None,
                is_active=True,
                revoked_at=None,
            )
        ]

    app.dependency_overrides[_current_universal_user] = override_current_user
    monkeypatch.setattr(UUIService, "get_grants", fake_get_grants)

    with TestClient(app) as client:
        response = client.get("/v1/uui/me")

    assert response.status_code == 200
    payload = response.json()["data"]
    assert payload["email"] == "alex@example.com"
    assert payload["memory_count"] == 2
    assert payload["grants"][0]["agent_name"] == "Docs Agent"
    assert payload["grants"][0]["agent_website_url"] == "https://docs.example.com"
    assert payload["grants"][0]["agent_is_verified"] is True


def test_create_grant_with_session_user_returns_grant(monkeypatch) -> None:
    app = _build_test_app()
    user_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    resolved_user = SimpleNamespace(
        id=user_id,
        uui_token="uui_" + ("a" * 48),
        email="alex@example.com",
        display_name="Alex",
        created_at=datetime.now(UTC),
        is_active=True,
        memory_count=1,
    )

    async def override_current_user():
        return resolved_user

    async def fake_create_grant(
        self,
        user_uui_id: str,
        agent_id: str,
        categories: list[str],
        access_type: str,
        expires_at=None,
    ):
        return SimpleNamespace(
            id=uuid.uuid4(),
            user_uui_id=uuid.UUID(user_uui_id),
            agent_id=uuid.UUID(agent_id),
            global_agent=SimpleNamespace(
                name="Consent Agent",
                logo_url=None,
                website_url="https://consent.example.com",
                is_verified=False,
            ),
            categories_allowed=categories,
            access_type=access_type,
            granted_at=datetime.now(UTC),
            expires_at=expires_at,
            is_active=True,
            revoked_at=None,
        )

    async def fake_send_grant_notification(
        self,
        to_email: str,
        agent_name: str,
        categories: list[str],
        manage_url: str,
        expires_at=None,
    ):
        return True

    app.dependency_overrides[_current_universal_user] = override_current_user
    monkeypatch.setattr(UUIService, "create_grant", fake_create_grant)
    monkeypatch.setattr("api.routers.uui.EmailService.send_grant_notification", fake_send_grant_notification)

    with TestClient(app) as client:
        response = client.post(
            "/v1/uui/me/grants",
            json={
                "agent_id": str(agent_id),
                "categories_allowed": ["preference"],
                "access_type": "read_only",
                "expires_at": datetime.now(UTC).isoformat(),
            },
        )

    assert response.status_code == 200
    payload = response.json()["data"]
    assert payload["categories_allowed"] == ["preference"]
    assert payload["access_type"] == "read_only"


def test_revoke_grant_with_session_user_returns_revoked(monkeypatch) -> None:
    app = _build_test_app()
    user_id = uuid.uuid4()
    grant_id = uuid.uuid4()
    expected_grant_id = str(grant_id)
    resolved_user = SimpleNamespace(
        id=user_id,
        uui_token="uui_" + ("a" * 48),
        email="alex@example.com",
        display_name="Alex",
        created_at=datetime.now(UTC),
        is_active=True,
        memory_count=1,
    )

    async def override_current_user():
        return resolved_user

    async def fake_revoke_grant(self, user_uui_id: str, grant_id: str):
        return user_uui_id == str(user_id) and grant_id == expected_grant_id

    app.dependency_overrides[_current_universal_user] = override_current_user
    monkeypatch.setattr(UUIService, "revoke_grant", fake_revoke_grant)

    with TestClient(app) as client:
        response = client.delete(f"/v1/uui/me/grants/{grant_id}")

    assert response.status_code == 200
    assert response.json()["data"]["revoked"] is True
