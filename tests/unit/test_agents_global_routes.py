from __future__ import annotations

from datetime import UTC
from datetime import datetime
from types import SimpleNamespace
import uuid

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from api.db.database import get_db_session
from api.dependencies import get_authenticated_tenant_id
from api.errors import APIError
from api.routers.agents import router as agents_router
from api.services.global_agent_service import GlobalAgentService


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

    app.include_router(agents_router)
    app.dependency_overrides[get_db_session] = _override_db_session
    app.dependency_overrides[get_authenticated_tenant_id] = lambda: str(uuid.uuid4())
    return app


def test_register_global_agent_returns_agent_and_raw_key(monkeypatch) -> None:
    app = _build_test_app()
    tenant_id = str(uuid.uuid4())
    agent_id = uuid.uuid4()

    async def override_tenant_id():
        return tenant_id

    async def fake_register(
        self,
        tenant_id: str,
        name: str,
        description: str | None,
        logo_url: str | None,
        website_url: str | None,
        default_categories_requested: list[str] | None,
        redirect_uri: str | None = None,
    ):
        agent = SimpleNamespace(
            id=agent_id,
            owner_tenant_id=uuid.UUID(tenant_id),
            name=name,
            description=description,
            logo_url=logo_url,
            website_url=website_url,
            default_categories_requested=list(default_categories_requested or []),
            redirect_uri=redirect_uri,
            is_verified=False,
            is_public=True,
            created_at=datetime.now(UTC),
            is_active=True,
        )
        return agent, "agent_sk_test_key"

    app.dependency_overrides[get_authenticated_tenant_id] = override_tenant_id
    monkeypatch.setattr(GlobalAgentService, "register", fake_register)

    with TestClient(app) as client:
        response = client.post(
            "/v1/agents/global",
            json={
                "name": "Test Agent",
                "redirect_uri": "http://localhost:3001/callback",
                "default_categories_requested": ["preference", "expertise"],
            },
        )

    assert response.status_code == 200
    payload = response.json()["data"]
    assert payload["name"] == "Test Agent"
    assert payload["redirect_uri"] == "http://localhost:3001/callback"
    assert payload["raw_agent_api_key"] == "agent_sk_test_key"
