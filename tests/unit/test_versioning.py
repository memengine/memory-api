from __future__ import annotations

from datetime import UTC
from datetime import datetime
from datetime import timedelta

from fastapi import FastAPI
from fastapi import Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from api.middleware.versioning import DeprecatedVersionInfo
from api.middleware.versioning import VersioningMiddleware
from api.middleware.versioning import register_deprecated_field


def test_versioning_attaches_api_version_from_url() -> None:
    app = FastAPI()
    app.add_middleware(VersioningMiddleware)

    @app.get("/v1/ping")
    async def ping(request: Request):
        return {"version": request.state.api_version}

    with TestClient(app) as client:
        response = client.get("/v1/ping")

    assert response.status_code == 200
    assert response.json() == {"version": 1}


def test_versioning_rejects_unsupported_versions() -> None:
    app = FastAPI()
    app.add_middleware(VersioningMiddleware)

    with TestClient(app) as client:
        response = client.get("/v2/anything")

    assert response.status_code == 400
    assert response.json() == {
        "error": "unsupported_api_version",
        "max_supported": "v1",
    }


def test_deprecated_version_adds_standard_headers(monkeypatch) -> None:
    app = FastAPI()
    monkeypatch.setattr(
        "api.middleware.versioning.DEPRECATED_API_VERSIONS",
        {
            1: DeprecatedVersionInfo(
                sunset_at=datetime.now(UTC) + timedelta(days=200),
                migration_guide_url="https://docs.memoryos.io/migration/v1-to-v2",
                successor_version="v2",
            )
        },
    )
    app.add_middleware(VersioningMiddleware)

    @app.get("/v1/ping")
    async def ping():
        return {"ok": True}

    with TestClient(app) as client:
        response = client.get("/v1/ping")

    assert response.status_code == 200
    assert response.headers["Deprecation"] == "true"
    assert "https://docs.memoryos.io/migration/v1-to-v2" in response.headers["Link"]
    assert "Sunset" in response.headers


def test_deprecated_field_header_is_added(monkeypatch) -> None:
    recorded = []

    async def fake_record(self, request, deprecated_fields):
        recorded.append(
            (
                str(getattr(request.state, "tenant_id", "")),
                [notice.field_path for notice in deprecated_fields],
            )
        )

    monkeypatch.setattr(VersioningMiddleware, "_record_deprecated_usage", fake_record)

    app = FastAPI()
    app.add_middleware(VersioningMiddleware)

    @app.get("/v1/test")
    async def deprecated_response(request: Request):
        request.state.tenant_id = "00000000-0000-0000-0000-000000000123"
        register_deprecated_field(
            request,
            field_path="GET /v1/test response.user_id",
            header_field_name="user_id",
            sunset_at=datetime(2026, 10, 1, tzinfo=UTC),
            migration_guide_url="/docs/migration",
            replacement_field="external_user_id",
        )
        return JSONResponse({"user_id": "legacy"})

    with TestClient(app) as client:
        response = client.get("/v1/test")

    assert response.status_code == 200
    assert response.headers["X-MemoryOS-Deprecated-Fields"] == (
        "user_id (sunset: 2026-10-01); see /docs/migration"
    )
    assert recorded == [
        (
            "00000000-0000-0000-0000-000000000123",
            ["GET /v1/test response.user_id"],
        )
    ]
