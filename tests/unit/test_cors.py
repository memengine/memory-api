from __future__ import annotations

from fastapi.testclient import TestClient

from api.main import create_app


def test_preflight_from_allowed_origin_returns_cors_headers(monkeypatch) -> None:
    monkeypatch.setenv(
        "CORS_ALLOWED_ORIGINS",
        "http://localhost:3000,http://localhost:3001",
    )
    app = create_app()

    with TestClient(app) as client:
        response = client.options(
            "/v1/memories",
            headers={
                "Origin": "http://localhost:3000",
                "Access-Control-Request-Method": "GET",
                "Access-Control-Request-Headers": "authorization,content-type",
            },
        )

    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == "http://localhost:3000"


def test_unknown_origin_does_not_get_allow_origin_header(monkeypatch) -> None:
    monkeypatch.setenv(
        "CORS_ALLOWED_ORIGINS",
        "http://localhost:3000,http://localhost:3001",
    )
    app = create_app()

    with TestClient(app) as client:
        response = client.options(
            "/v1/memories",
            headers={
                "Origin": "http://malicious.example",
                "Access-Control-Request-Method": "GET",
                "Access-Control-Request-Headers": "authorization,content-type",
            },
        )

    assert "access-control-allow-origin" not in response.headers


def test_exposed_headers_include_quota_mode(monkeypatch) -> None:
    monkeypatch.setenv(
        "CORS_ALLOWED_ORIGINS",
        "http://localhost:3000,http://localhost:3001",
    )
    app = create_app()

    with TestClient(app) as client:
        response = client.get(
            "/health",
            headers={"Origin": "http://localhost:3000"},
        )

    exposed_headers = response.headers.get("access-control-expose-headers", "")
    assert "X-MemoryOS-Quota-Mode" in exposed_headers
