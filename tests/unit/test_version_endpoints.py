from __future__ import annotations

import uuid
from datetime import UTC
from datetime import datetime

from fastapi import FastAPI
from fastapi.testclient import TestClient
from fastapi.responses import JSONResponse

from api.db.database import get_db_session
from api.dependencies import get_authenticated_tenant_id
from api.dependencies import get_cache_service
from api.errors import APIError
from api.routers.memories import router as memories_router
from api.routers.users import router as users_router
from api.services.version_service import UserDataExport
from api.services.version_service import VersionService


async def _override_db_session():
    yield object()


class FakeCache:
    def __init__(self):
        self.counts: dict[str, int] = {}

    async def increment_rate_counter(self, key: str, window_seconds: int = 60) -> int:
        self.counts[key] = self.counts.get(key, 0) + 1
        return self.counts[key]


def _build_app(cache: FakeCache | None = None) -> FastAPI:
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

    app.include_router(memories_router)
    app.include_router(users_router)
    app.dependency_overrides[get_db_session] = _override_db_session
    app.dependency_overrides[get_authenticated_tenant_id] = lambda: str(uuid.uuid4())
    app.dependency_overrides[get_cache_service] = lambda: cache or FakeCache()
    return app


def test_memory_history_endpoint_returns_created_version(monkeypatch) -> None:
    memory_id = uuid.uuid4()
    created_at = datetime.now(UTC)

    async def fake_get_history(self, *, memory_id: str, tenant_id: str):
        return [
            type(
                "Version",
                (),
                {
                    "version_number": 1,
                    "content": "User prefers concise answers",
                    "change_type": "created",
                    "change_reason": "Extracted from conversation",
                    "created_at": created_at,
                },
            )()
        ]

    monkeypatch.setattr(VersionService, "get_history", fake_get_history)

    with TestClient(_build_app()) as client:
        response = client.get(f"/v1/memories/{memory_id}/history")

    assert response.status_code == 200
    payload = response.json()
    assert payload["data"][0]["version_number"] == 1
    assert payload["data"][0]["change_type"] == "created"
    assert payload["data"][0]["change_reason"] == "Extracted from conversation"


def test_memory_history_endpoint_returns_conflict_update_reason(monkeypatch) -> None:
    original_id = uuid.uuid4()
    created_at = datetime.now(UTC)

    async def fake_get_history(self, *, memory_id: str, tenant_id: str):
        return [
            type(
                "Version",
                (),
                {
                    "version_number": 1,
                    "content": "User prefers Python",
                    "change_type": "created",
                    "change_reason": "Extracted from conversation",
                    "created_at": created_at,
                },
            )(),
            type(
                "Version",
                (),
                {
                    "version_number": 2,
                    "content": "User prefers Python",
                    "change_type": "conflict_update",
                    "change_reason": "Superseded by: User switched to Go for backend services",
                    "created_at": created_at,
                },
            )(),
        ]

    monkeypatch.setattr(VersionService, "get_history", fake_get_history)

    with TestClient(_build_app()) as client:
        response = client.get(f"/v1/memories/{original_id}/history")

    assert response.status_code == 200
    payload = response.json()
    assert payload["data"][1]["version_number"] == 2
    assert payload["data"][1]["change_type"] == "conflict_update"
    assert "User switched to Go" in payload["data"][1]["change_reason"]


def test_user_export_endpoint_returns_versions_and_rate_limits(monkeypatch) -> None:
    cache = FakeCache()
    tenant_id = uuid.uuid4()
    proxy_user_id = uuid.uuid4()

    def override_tenant_id():
        return str(tenant_id)

    async def fake_execute(self, _statement):
        return type(
            "Result",
            (),
            {
                "scalar_one_or_none": lambda _self: type(
                    "ProxyUser",
                    (),
                    {"id": proxy_user_id},
                )()
            },
        )()

    async def fake_export(self, *, proxy_user_id: str, tenant_id: str):
        return UserDataExport(
            tenant_id=tenant_id,
            proxy_user_id=proxy_user_id,
            memories=[
                {
                    "id": str(uuid.uuid4()),
                    "content": "Active memory",
                    "is_archived": False,
                    "versions": [{"version_number": 1, "change_type": "created"}],
                },
                {
                    "id": str(uuid.uuid4()),
                    "content": "Archived memory",
                    "is_archived": True,
                    "versions": [
                        {"version_number": 1, "change_type": "created"},
                        {"version_number": 2, "change_type": "archived"},
                    ],
                },
            ],
        )

    monkeypatch.setattr(VersionService, "get_user_data_export", fake_export)
    app = _build_app(cache)
    app.dependency_overrides[get_authenticated_tenant_id] = override_tenant_id

    class FakeSession:
        async def execute(self, statement):
            return await fake_execute(self, statement)

    async def override_db_session():
        yield FakeSession()

    app.dependency_overrides[get_db_session] = override_db_session

    with TestClient(app) as client:
        first = client.get("/v1/users/student-1/export")
        second = client.get("/v1/users/student-1/export")

    assert first.status_code == 200
    payload = first.json()
    assert len(payload["data"]["memories"]) == 2
    assert [memory["is_archived"] for memory in payload["data"]["memories"]] == [False, True]
    assert all("versions" in memory for memory in payload["data"]["memories"])
    assert second.status_code == 429
    assert second.json()["code"] == "EXP_429"
