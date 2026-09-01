from __future__ import annotations

import uuid
from datetime import UTC
from datetime import datetime
from types import SimpleNamespace

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
from api.tasks.universal_extraction_tasks import UNIVERSAL_COLLECTION_NAME
from api.tasks.universal_extraction_tasks import run_universal_extraction_pipeline


class FakeSessionContext:
    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        return None


class FakeSessionFactory:
    def __call__(self):
        return FakeSessionContext()


class FakeAgentService:
    def __init__(self, *args, **kwargs) -> None:
        return None

    async def resolve_from_api_key(self, raw_key: str):
        if raw_key == "agent_sk_valid":
            return SimpleNamespace(
                id=uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"),
                owner_tenant_id=uuid.uuid4(),
                name="Agent A",
                is_active=True,
            )
        if raw_key == "agent_sk_other":
            return SimpleNamespace(
                id=uuid.UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"),
                owner_tenant_id=uuid.uuid4(),
                name="Agent B",
                is_active=True,
            )
        return None


class FakeUUIAuthService:
    def __init__(self, *args, **kwargs) -> None:
        return None

    async def resolve_by_token(self, token: str):
        if token == "uui_user_a":
            return SimpleNamespace(
                id=uuid.UUID("11111111-1111-1111-1111-111111111111"),
                uui_token=token,
                is_active=True,
            )
        if token == "uui_user_b":
            return SimpleNamespace(
                id=uuid.UUID("22222222-2222-2222-2222-222222222222"),
                uui_token=token,
                is_active=True,
            )
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


async def _override_db_session():
    yield SimpleNamespace()


def _build_app() -> FastAPI:
    app = FastAPI()
    app.add_middleware(
        UniversalAuthMiddleware,
        session_factory=FakeSessionFactory(),
        global_agent_service_factory=FakeAgentService,
        uui_service_factory=FakeUUIAuthService,
    )
    app.include_router(router)
    app.dependency_overrides[get_db_session] = _override_db_session
    app.dependency_overrides[get_cache_service] = lambda: SimpleNamespace()
    app.dependency_overrides[get_qdrant_service] = lambda: SimpleNamespace()
    app.dependency_overrides[get_context_builder] = lambda: ContextBuilder()
    app.dependency_overrides[get_quality_gate_service] = lambda: FakeQualityGateService()
    return app


def test_retrieve_no_grant_returns_empty_not_403(monkeypatch) -> None:
    app = _build_app()

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


def test_retrieve_respects_category_filter(monkeypatch) -> None:
    app = _build_app()

    async def fake_get_grants(self, user_uui_id: str):
        return [
            SimpleNamespace(
                agent_id=uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"),
                access_type="read_write",
                categories_allowed=["expertise"],
            )
        ]

    async def fake_search(**kwargs):
        assert kwargs["allowed_categories"] == ["expertise"]
        return (
            [
                MemorySearchResult(
                    id=str(uuid.uuid4()),
                    content="User knows graph theory well.",
                    category="expertise",
                    importance_score=7.0,
                    last_accessed=None,
                    relevance_score=0.9,
                    context_snippet="- User knows graph theory well.",
                )
            ],
            "What you know about this user:\n- User knows graph theory well.",
            10,
        )

    monkeypatch.setattr(UUIService, "get_grants", fake_get_grants)
    monkeypatch.setattr("api.routers.universal._search_universal_memories", fake_search)

    with TestClient(app) as client:
        response = client.post(
            "/v1/universal/memories/retrieve",
            json={"query": "what is the user good at?", "limit": 5},
            headers={
                "Authorization": "ApiKey agent_sk_valid",
                "X-MemoryOS-UUI": "uui_valid",
            },
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["categories_available"] == ["expertise"]
    assert payload["data"][0]["category"] == "expertise"


def test_write_with_read_only_grant_returns_403(monkeypatch) -> None:
    app = _build_app()

    async def fake_get_grants(self, user_uui_id: str):
        return [
            SimpleNamespace(
                agent_id=uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"),
                access_type="read_only",
                categories_allowed=["preference"],
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
    assert response.json()["code"] == "UAT_002"


def test_user_a_cannot_see_user_b_memories(monkeypatch) -> None:
    app = _build_app()
    seen_user_ids: list[str] = []

    async def fake_get_grants(self, user_uui_id: str):
        return [
            SimpleNamespace(
                agent_id=uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"),
                access_type="read_write",
                categories_allowed=["fact"],
            )
        ]

    async def fake_search(**kwargs):
        seen_user_ids.append(str(kwargs["user"].id))
        return (
            [
                MemorySearchResult(
                    id=str(uuid.uuid4()),
                    content=f"Scoped memory for {kwargs['user'].id}",
                    category="fact",
                    importance_score=6.0,
                    last_accessed=None,
                    relevance_score=0.88,
                    context_snippet="- Scoped memory",
                )
            ],
            "What you know about this user:\n- Scoped memory",
            10,
        )

    monkeypatch.setattr(UUIService, "get_grants", fake_get_grants)
    monkeypatch.setattr("api.routers.universal._search_universal_memories", fake_search)

    with TestClient(app) as client:
        response_a = client.post(
            "/v1/universal/memories/retrieve",
            json={"query": "what do you know?", "limit": 5},
            headers={
                "Authorization": "ApiKey agent_sk_valid",
                "X-MemoryOS-UUI": "uui_user_a",
            },
        )
        response_b = client.post(
            "/v1/universal/memories/retrieve",
            json={"query": "what do you know?", "limit": 5},
            headers={
                "Authorization": "ApiKey agent_sk_valid",
                "X-MemoryOS-UUI": "uui_user_b",
            },
        )

    assert response_a.status_code == 200
    assert response_b.status_code == 200
    assert seen_user_ids == [
        "11111111-1111-1111-1111-111111111111",
        "22222222-2222-2222-2222-222222222222",
    ]
    assert response_a.json()["data"][0]["content"] != response_b.json()["data"][0]["content"]


def test_agent_a_cannot_see_agent_b_memories_for_same_user(monkeypatch) -> None:
    app = _build_app()

    async def fake_get_grants(self, user_uui_id: str):
        return [
            SimpleNamespace(
                agent_id=uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"),
                access_type="read_write",
                categories_allowed=["fact"],
            ),
            SimpleNamespace(
                agent_id=uuid.UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"),
                access_type="read_write",
                categories_allowed=["relationship", "goal"],
            ),
        ]

    async def fake_search(**kwargs):
        assert kwargs["allowed_categories"] == ["fact"]
        return (
            [
                MemorySearchResult(
                    id=str(uuid.uuid4()),
                    content="User lives in Pune.",
                    category="fact",
                    importance_score=6.0,
                    last_accessed=None,
                    relevance_score=0.88,
                    context_snippet="- User lives in Pune.",
                )
            ],
            "What you know about this user:\n- User lives in Pune.",
            10,
        )

    monkeypatch.setattr(UUIService, "get_grants", fake_get_grants)
    monkeypatch.setattr("api.routers.universal._search_universal_memories", fake_search)

    with TestClient(app) as client:
        response = client.post(
            "/v1/universal/memories/retrieve",
            json={"query": "where does the user live?", "limit": 5},
            headers={
                "Authorization": "ApiKey agent_sk_valid",
                "X-MemoryOS-UUI": "uui_valid",
            },
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["categories_available"] == ["fact"]
    assert "relationship" not in payload["categories_available"]
    assert "goal" not in payload["categories_available"]
    assert "other_agents" not in payload


def test_universal_memories_not_in_tenant_endpoint() -> None:
    upsert_calls: list[dict[str, object]] = []
    created_sessions: list[FakeSession] = []

    class FakeSession:
        def __init__(self) -> None:
            self.added = []
            self.universal_user = SimpleNamespace(
                id=uuid.UUID("11111111-1111-1111-1111-111111111111"),
                is_active=True,
                memory_count=0,
            )

        def get(self, model, key):
            model_name = getattr(model, "__name__", str(model))
            if model_name == "UniversalUser":
                return self.universal_user
            return None

        def execute(self, query):
            return SimpleNamespace(
                scalar_one_or_none=lambda: SimpleNamespace(
                    categories_allowed=["fact"],
                    access_type="read_write",
                    is_active=True,
                )
            )

        def add(self, instance) -> None:
            self.added.append(instance)

        def flush(self) -> None:
            return None

        def commit(self) -> None:
            return None

        def rollback(self) -> None:
            return None

        def close(self) -> None:
            return None

    class FakeSessionFactory:
        def __call__(self):
            session = FakeSession()
            created_sessions.append(session)
            return session

    class FakeExtractor:
        def extract(self, messages, user_id):
            return [
                SimpleNamespace(
                    content="Shared universal fact",
                    category="fact",
                    importance_score=5.0,
                    confidence=0.91,
                    reasoning="explicit fact",
                    expiry="permanent",
                )
            ]

    class FakeScorer:
        def score(self, memory, context):
            return 7.5

    class FakeEmbeddingService:
        def __init__(self, sync_session=None) -> None:
            return None

        def embed_sync(self, content: str):
            return SimpleNamespace(vector=[0.1, 0.2, 0.3], dimensions=3)

    class FakeQdrantService:
        def upsert_memory(self, **kwargs):
            upsert_calls.append(kwargs)
            return True

    job_payload = {
        "job_id": str(uuid.uuid4()),
        "user_uui_id": "11111111-1111-1111-1111-111111111111",
        "agent_id": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
        "messages": [{"role": "user", "content": "remember this universal fact"}],
        "metadata": {},
        "queued_at": datetime.now(UTC).isoformat(),
    }

    original_embedding_service = run_universal_extraction_pipeline.__globals__["EmbeddingService"]
    run_universal_extraction_pipeline.__globals__["EmbeddingService"] = FakeEmbeddingService
    try:
        result = run_universal_extraction_pipeline(
            job_payload,
            session_factory=FakeSessionFactory(),
            extractor=FakeExtractor(),
            scorer=FakeScorer(),
            qdrant_service=FakeQdrantService(),
        )
    finally:
        run_universal_extraction_pipeline.__globals__["EmbeddingService"] = original_embedding_service

    assert result["status"] == "processed"
    assert result["memories_created"] == 1
    outbox_rows = [item for item in created_sessions[-1].added if getattr(item, "payload", {}).get("qdrant_collection") == UNIVERSAL_COLLECTION_NAME]
    assert len(outbox_rows) == 1


def test_tenant_memories_not_in_universal_endpoint(monkeypatch) -> None:
    app = _build_app()

    async def fake_get_grants(self, user_uui_id: str):
        return [
            SimpleNamespace(
                agent_id=uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"),
                access_type="read_write",
                categories_allowed=["fact"],
            )
        ]

    async def fake_search(**kwargs):
        assert kwargs["allowed_categories"] == ["fact"]
        return ([], "", 0)

    monkeypatch.setattr(UUIService, "get_grants", fake_get_grants)
    monkeypatch.setattr("api.routers.universal._search_universal_memories", fake_search)

    with TestClient(app) as client:
        response = client.post(
            "/v1/universal/memories/retrieve",
            json={"query": "tenant memory should not appear", "limit": 5},
            headers={
                "Authorization": "ApiKey agent_sk_valid",
                "X-MemoryOS-UUI": "uui_valid",
            },
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["data"] == []
    assert payload["categories_available"] == ["fact"]
