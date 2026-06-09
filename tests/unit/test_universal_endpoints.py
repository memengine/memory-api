from __future__ import annotations

import uuid
from types import SimpleNamespace
from typing import Any

from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.db.database import get_db_session
from api.dependencies import get_cache_service
from api.dependencies import get_context_builder
from api.dependencies import get_qdrant_service
from api.dependencies import get_quality_gate_service
from api.middleware.universal_auth import UniversalAuthMiddleware
from api.routers.universal import router
from api.schemas.responses import MemorySearchResult
from api.services.context_builder import ContextBuilder
from api.services.quality_gate import GateResult
from api.services.uui_service import UUIService


class FakeSessionContext:
    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        return None


class FakeSessionFactory:
    def __call__(self):
        return FakeSessionContext()


class FakeGlobalAgentService:
    def __init__(self, *args, **kwargs) -> None:
        return None

    async def resolve_from_api_key(self, raw_key: str):
        if raw_key == "agent_sk_valid":
            return SimpleNamespace(
                id=uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"),
                owner_tenant_id=uuid.uuid4(),
                name="Docs Agent",
                is_active=True,
            )
        return None


class FakeUUIAuthService:
    def __init__(self, *args, **kwargs) -> None:
        return None

    async def resolve_by_token(self, token: str):
        if token == "uui_valid":
            return SimpleNamespace(
                id=uuid.UUID("11111111-1111-1111-1111-111111111111"),
                uui_token=token,
                is_active=True,
            )
        return None


class FakeQualityGateService:
    async def check(self, messages, tenant_id, external_user_id):
        return GateResult(
            passed=True,
            blocked_layer=None,
            reason=None,
            budget_remaining_pct=1.0,
        )


class FakeCacheService:
    def __init__(self) -> None:
        self.values: dict[tuple[str, str, str], dict[str, Any]] = {}

    async def get_idempotent_response(
        self,
        key: str,
        *,
        scope: str = "legacy",
        operation: str = "memory_add",
    ):
        return self.values.get((scope, operation, key))

    async def set_idempotent_response(
        self,
        key: str,
        payload: dict[str, Any],
        ttl: int = 86400,
        *,
        scope: str = "legacy",
        operation: str = "memory_add",
    ) -> None:
        del ttl
        self.values[(scope, operation, key)] = dict(payload)


async def _override_db_session():
    yield SimpleNamespace()


def _build_test_app() -> FastAPI:
    app = FastAPI()
    app.add_middleware(
        UniversalAuthMiddleware,
        session_factory=FakeSessionFactory(),
        global_agent_service_factory=FakeGlobalAgentService,
        uui_service_factory=FakeUUIAuthService,
    )
    app.include_router(router)
    app.dependency_overrides[get_db_session] = _override_db_session
    app.dependency_overrides[get_cache_service] = lambda: FakeCacheService()
    app.dependency_overrides[get_qdrant_service] = lambda: SimpleNamespace()
    app.dependency_overrides[get_context_builder] = lambda: ContextBuilder()
    app.dependency_overrides[get_quality_gate_service] = lambda: FakeQualityGateService()
    return app


def test_universal_auth_invalid_credentials_return_uat_001() -> None:
    app = _build_test_app()

    with TestClient(app) as client:
        response = client.post(
            "/v1/universal/memories/add",
            json={"messages": [{"role": "user", "content": "remember this"}]},
            headers={
                "Authorization": "ApiKey agent_sk_invalid",
                "X-MemoryOS-UUI": "uui_invalid",
            },
        )

    assert response.status_code == 403
    assert response.json()["error"] == "cross_agent_auth_failed"
    assert response.json()["code"] == "UAT_001"


def test_universal_add_accepts_idempotency_key_field(monkeypatch) -> None:
    app = _build_test_app()
    agent_id = uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")

    async def fake_get_grants(self, user_uui_id: str):
        return [
            SimpleNamespace(
                agent_id=agent_id,
                access_type="read_write",
                categories_allowed=["preference"],
            )
        ]

    async def fake_dispatch(job_payload: dict[str, Any]) -> str | None:
        assert job_payload["messages"][0]["content"] == "remember this"
        assert "uui_token" not in job_payload
        return None

    monkeypatch.setattr(UUIService, "get_grants", fake_get_grants)
    monkeypatch.setattr("api.routers.universal._dispatch_universal_job", fake_dispatch)

    with TestClient(app) as client:
        response = client.post(
            "/v1/universal/memories/add",
            json={
                "messages": [{"role": "user", "content": "remember this"}],
                "metadata": {"source": "unit-test"},
                "idempotency_key": "idem-123",
            },
            headers={
                "Authorization": "ApiKey agent_sk_valid",
                "X-MemoryOS-UUI": "uui_valid",
            },
        )

    assert response.status_code == 200
    assert response.json()["status"] == "queued"


def test_universal_add_reuses_scoped_idempotent_job(monkeypatch) -> None:
    app = _build_test_app()
    agent_id = uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
    dispatches = 0

    async def fake_get_grants(self, user_uui_id: str):
        return [
            SimpleNamespace(
                agent_id=agent_id,
                access_type="read_write",
                categories_allowed=["preference"],
            )
        ]

    async def fake_dispatch(job_payload: dict[str, Any]) -> str | None:
        nonlocal dispatches
        dispatches += 1
        return None

    cache = FakeCacheService()
    app.dependency_overrides[get_cache_service] = lambda: cache
    monkeypatch.setattr(UUIService, "get_grants", fake_get_grants)
    monkeypatch.setattr("api.routers.universal._dispatch_universal_job", fake_dispatch)

    request = {
        "messages": [{"role": "user", "content": "remember this"}],
        "idempotency_key": "idem-123",
    }
    headers = {
        "Authorization": "ApiKey agent_sk_valid",
        "X-MemoryOS-UUI": "uui_valid",
    }
    with TestClient(app) as client:
        first = client.post("/v1/universal/memories/add", json=request, headers=headers)
        second = client.post("/v1/universal/memories/add", json=request, headers=headers)

    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json()["job_id"] == second.json()["job_id"]
    assert dispatches == 1


def test_universal_add_rejects_read_only_grant(monkeypatch) -> None:
    app = _build_test_app()
    agent_id = uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")

    async def fake_get_grants(self, user_uui_id: str):
        return [
            SimpleNamespace(
                agent_id=agent_id,
                access_type="read_only",
                categories_allowed=["fact"],
            )
        ]

    monkeypatch.setattr(UUIService, "get_grants", fake_get_grants)

    with TestClient(app) as client:
        response = client.post(
            "/v1/universal/memories/add",
            json={"messages": [{"role": "user", "content": "remember this"}]},
            headers={
                "Authorization": "ApiKey agent_sk_valid",
                "X-MemoryOS-UUI": "uui_valid",
            },
        )

    assert response.status_code == 403
    assert response.json()["error"] == "write_not_permitted"
    assert response.json()["code"] == "UAT_002"


def test_universal_retrieve_returns_empty_without_grant(monkeypatch) -> None:
    app = _build_test_app()

    async def fake_get_grants(self, user_uui_id: str):
        return []

    monkeypatch.setattr(UUIService, "get_grants", fake_get_grants)

    with TestClient(app) as client:
        response = client.post(
            "/v1/universal/memories/retrieve",
            json={"query": "what do you know?", "limit": 5},
            headers={
                "Authorization": "ApiKey agent_sk_valid",
                "X-MemoryOS-UUI": "uui_valid",
            },
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["data"] == []
    assert payload["permission_error"] == "no_grant_for_user"
    assert payload["categories_available"] == []


def test_universal_retrieve_returns_categories_and_results(monkeypatch) -> None:
    app = _build_test_app()
    agent_id = uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")

    async def fake_get_grants(self, user_uui_id: str):
        return [
            SimpleNamespace(
                agent_id=agent_id,
                access_type="read_write",
                categories_allowed=["fact", "preference"],
            )
        ]

    async def fake_search(**kwargs: Any):
        return (
            [
                MemorySearchResult(
                    id=str(uuid.uuid4()),
                    content="User prefers concise answers.",
                    category="preference",
                    importance_score=8.0,
                    last_accessed=None,
                    relevance_score=0.92,
                    context_snippet="- User prefers concise answers.",
                )
            ],
            "What you know about this user:\n- User prefers concise answers.",
            12,
        )

    monkeypatch.setattr(UUIService, "get_grants", fake_get_grants)
    monkeypatch.setattr("api.routers.universal._search_universal_memories", fake_search)

    with TestClient(app) as client:
        response = client.post(
            "/v1/universal/memories/retrieve",
            json={"query": "what do you know?", "limit": 5},
            headers={
                "Authorization": "ApiKey agent_sk_valid",
                "X-MemoryOS-UUI": "uui_valid",
            },
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["categories_available"] == ["fact", "preference"]
    assert payload["context_token_count"] == 12
    assert len(payload["data"]) == 1
    assert payload["data"][0]["category"] == "preference"


def test_universal_retrieve_returns_empty_after_grant_revocation(monkeypatch) -> None:
    app = _build_test_app()
    agent_id = uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
    call_count = {"count": 0}

    async def fake_get_grants(self, user_uui_id: str):
        call_count["count"] += 1
        if call_count["count"] == 1:
            return [
                SimpleNamespace(
                    agent_id=agent_id,
                    access_type="read_write",
                    categories_allowed=["fact"],
                )
            ]
        return []

    async def fake_search(**kwargs: Any):
        return (
            [
                MemorySearchResult(
                    id=str(uuid.uuid4()),
                    content="User works on backend APIs.",
                    category="fact",
                    importance_score=7.0,
                    last_accessed=None,
                    relevance_score=0.9,
                    context_snippet="- User works on backend APIs.",
                )
            ],
            "What you know about this user:\n- User works on backend APIs.",
            11,
        )

    monkeypatch.setattr(UUIService, "get_grants", fake_get_grants)
    monkeypatch.setattr("api.routers.universal._search_universal_memories", fake_search)

    headers = {
        "Authorization": "ApiKey agent_sk_valid",
        "X-MemoryOS-UUI": "uui_valid",
    }

    with TestClient(app) as client:
        first_response = client.post(
            "/v1/universal/memories/retrieve",
            json={"query": "what do you know?", "limit": 5},
            headers=headers,
        )
        second_response = client.post(
            "/v1/universal/memories/retrieve",
            json={"query": "what do you know?", "limit": 5},
            headers=headers,
        )

    assert first_response.status_code == 200
    assert len(first_response.json()["data"]) == 1

    assert second_response.status_code == 200
    second_payload = second_response.json()
    assert second_payload["data"] == []
    assert second_payload["permission_error"] == "no_grant_for_user"
    assert second_payload["categories_available"] == []
