from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from api.db.database import get_db_session
from api.dependencies import require_memory_passport_enabled
from api.errors import APIError
from api.main import create_app
from api.routers.agents import router as agents_router
from api.settings import Settings
from api.settings import get_settings


@pytest.fixture(autouse=True)
def clear_settings_cache() -> None:
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _route_paths() -> set[str]:
    paths: set[str] = set()
    pending = list(create_app().routes)
    while pending:
        route = pending.pop()
        path = getattr(route, "path", None)
        if path is not None:
            paths.add(path)
        original_router = getattr(route, "original_router", None)
        if original_router is not None:
            pending.extend(original_router.routes)
    return paths


def test_memory_passport_is_disabled_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MEMORY_PASSPORT_ENABLED", "false")

    settings = get_settings()

    assert Settings(_env_file=None).memory_passport_enabled is False
    assert settings.memory_passport_enabled is False
    with pytest.raises(APIError) as error:
        require_memory_passport_enabled()
    assert error.value.status_code == 404
    assert error.value.code == "FEATURE_404"
    assert error.value.error == "memory_passport_not_available"

    app = create_app()
    paths = _route_paths()
    assert "/v1/uui/register" not in paths
    assert "/v1/universal/memories/retrieve" not in paths
    assert "/v1/agents/global" not in app.openapi()["paths"]
    assert "/v1/tenant/memory-passport/link-token" not in app.openapi()["paths"]
    assert "/v1/mcp/universal/capability" not in app.openapi()["paths"]


def test_disabled_global_agent_route_returns_feature_not_found(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MEMORY_PASSPORT_ENABLED", "false")
    get_settings.cache_clear()
    app = FastAPI()

    @app.exception_handler(APIError)
    async def api_error_handler(_request, exc: APIError):
        return JSONResponse(
            status_code=exc.status_code,
            content={"code": exc.code, "error": exc.error},
        )

    async def override_db_session():
        yield object()

    app.include_router(agents_router)
    app.dependency_overrides[get_db_session] = override_db_session

    response = TestClient(app).get(
        "/v1/agents/global/123e4567-e89b-12d3-a456-426614174000"
    )

    assert response.status_code == 404
    assert response.json() == {
        "code": "FEATURE_404",
        "error": "memory_passport_not_available",
    }


def test_memory_passport_routes_can_be_enabled_explicitly(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MEMORY_PASSPORT_ENABLED", "true")
    get_settings.cache_clear()

    require_memory_passport_enabled()
    paths = _route_paths()

    assert "/v1/uui/register" in paths
    assert "/v1/universal/memories/retrieve" in paths
