from __future__ import annotations

import os
import uuid
from datetime import UTC
from datetime import datetime
from types import SimpleNamespace

from fastapi.testclient import TestClient

from api import dependencies
from api.db.database import get_db_session
from api.main import create_app
from api.middleware.auth import AuthMiddleware
from api.settings import get_settings
from api.services.quality_gate import GateResult
from api.services.retriever import MemoryResult
from api.schemas.tenant_schemas import CostSummary
from api.schemas.tenant_schemas import ProxyUserDetail


def make_memory(
    *,
    content: str,
    category: str = "preference",
    importance_score: float = 8.0,
) -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid.uuid4(),
        content=content,
        category=SimpleNamespace(value=category),
        importance_score=importance_score,
        confidence_score=0.9,
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
        last_accessed_at=datetime.now(UTC),
        access_count=3,
        is_archived=False,
        agent_id=None,
        previous_version_id=None,
        source_conversation_id=uuid.uuid4(),
        metadata_json={"source": "test"},
    )


class StubMemoryService:
    def __init__(self) -> None:
        self.memories = [
            make_memory(content="User prefers Python"),
            make_memory(content="User prefers FastAPI"),
            make_memory(content="User prefers PostgreSQL"),
        ]
        self.idempotency_jobs: dict[tuple[str, str], dict[str, str]] = {}

    async def list_memories(self, **kwargs):
        cursor = kwargs.get("cursor")
        limit = int(kwargs.get("limit", 10))
        return _slice_with_cursor(self.memories, cursor=cursor, limit=limit)

    async def queue_memory_add(self, **kwargs):
        idempotency_key = kwargs.get("idempotency_key")
        if idempotency_key:
            scoped_key = (str(kwargs.get("tenant_id") or ""), str(idempotency_key))
            if scoped_key not in self.idempotency_jobs:
                self.idempotency_jobs[scoped_key] = {
                    "job_id": f"job_{len(self.idempotency_jobs) + 1}",
                    "status": "queued",
                }
            return self.idempotency_jobs[scoped_key]
        return {"job_id": f"job_{uuid.uuid4().hex[:8]}", "status": "queued"}

    async def get_idempotent_memory_add(self, **kwargs):
        scoped_key = (
            str(kwargs.get("tenant_id") or ""),
            str(kwargs.get("idempotency_key") or ""),
        )
        return self.idempotency_jobs.get(scoped_key)

    async def get_memory(self, **kwargs):
        return self.memories[0]

    async def update_memory(self, **kwargs):
        return self.memories[0]

    async def delete_memory(self, **kwargs):
        return True

    async def get_job_status(self, **kwargs):
        return {
            "job_id": kwargs["job_id"],
            "status": "queued",
            "memories_created": 0,
            "attempts": 1,
            "queue_name": "starter-extraction",
            "error": None,
            "queued_at": "2026-04-02T00:00:00+00:00",
            "started_at": None,
            "completed_at": None,
            "dead_lettered_at": None,
            "extraction_metadata": {"compositional_pass_attempted": False},
        }


class StubRetrieverService:
    def __init__(self) -> None:
        self.last_cache_hit = False

    async def retrieve(self, **kwargs):
        return [
            MemoryResult(
                id="mem_uuid",
                content="User builds SaaS products using Python and FastAPI",
                category="expertise",
                importance_score=7.8,
                confidence_score=0.95,
                semantic_score=0.93,
                recency_score=1.0,
                final_score=0.92,
                agent_id=None,
                previous_version_id=None,
                last_accessed_at="2026-03-15T10:30:00+00:00",
                created_at="2026-03-15T10:00:00+00:00",
            )
        ]


class StubUserService:
    async def get_profile(self, **kwargs):
        user = SimpleNamespace(
            id=uuid.uuid4(),
            external_id="user_abc123",
            email="user@example.com",
            settings={"tone": "concise"},
        )
        return user, 2, 128

    async def update_settings(self, **kwargs):
        user = SimpleNamespace(
            id=uuid.uuid4(),
            external_id="user_abc123",
            email="user@example.com",
            settings=kwargs["settings"],
        )
        return user, 2, 128

    async def export_user_data(self, **kwargs):
        user = SimpleNamespace(
            id=uuid.uuid4(),
            external_id="user_abc123",
            email="user@example.com",
            settings={"tone": "concise"},
        )
        memory = make_memory(content="User prefers Python")
        api_key = SimpleNamespace(
            id=uuid.uuid4(),
            name="Primary SDK",
            permissions=["read"],
            rate_limit_per_minute=60,
            created_at=datetime.now(UTC),
            last_used_at=None,
            is_active=True,
        )
        agent = SimpleNamespace(
            id=uuid.uuid4(),
            name="assistant",
            description="Main agent",
            memory_scope=SimpleNamespace(value="private"),
            created_at=datetime.now(UTC),
        )
        return user, 1, 64, [memory], [api_key], [agent]

    async def delete_user(self, **kwargs):
        return True, 2


class StubApiKeyService:
    def __init__(self) -> None:
        self.api_keys = [
            SimpleNamespace(
                id=uuid.uuid4(),
                name="Primary SDK",
                permissions=["read"],
                rate_limit_per_minute=60,
                created_at=datetime.now(UTC),
                last_used_at=None,
                is_active=True,
            ),
            SimpleNamespace(
                id=uuid.uuid4(),
                name="Secondary SDK",
                permissions=["write"],
                rate_limit_per_minute=120,
                created_at=datetime.now(UTC),
                last_used_at=None,
                is_active=True,
            ),
        ]

    async def list_api_keys(self, **kwargs):
        cursor = kwargs.get("cursor")
        limit = int(kwargs.get("limit", 10))
        return _slice_with_cursor(self.api_keys, cursor=cursor, limit=limit)

    async def create_api_key(self, **kwargs):
        api_key = SimpleNamespace(
            id=uuid.uuid4(),
            name=kwargs["name"],
            permissions=kwargs["permissions"],
            rate_limit_per_minute=kwargs["rate_limit_per_minute"],
            created_at=datetime.now(UTC),
            last_used_at=None,
            is_active=True,
        )
        return api_key, "mem_live_key"

    async def revoke_api_key(self, **kwargs):
        return True


class StubAgentService:
    def __init__(self) -> None:
        self.agents = [
            SimpleNamespace(
                id=uuid.uuid4(),
                name="assistant",
                description="Main agent",
                memory_scope=SimpleNamespace(value="private"),
                created_at=datetime.now(UTC),
            ),
            SimpleNamespace(
                id=uuid.uuid4(),
                name="coding-agent",
                description="Coding helper",
                memory_scope=SimpleNamespace(value="shared"),
                created_at=datetime.now(UTC),
            ),
        ]

    async def list_agents(self, **kwargs):
        cursor = kwargs.get("cursor")
        limit = int(kwargs.get("limit", 10))
        return _slice_with_cursor(self.agents, cursor=cursor, limit=limit)

    async def create_agent(self, **kwargs):
        return SimpleNamespace(
            id=uuid.uuid4(),
            name=kwargs["name"],
            description=kwargs["description"],
            memory_scope=SimpleNamespace(value=kwargs["memory_scope"]),
            created_at=datetime.now(UTC),
        )


class StubWebhookService:
    async def verify_and_process(self, **kwargs):
        return True


class StubProxyUserService:
    async def resolve(self, **kwargs):
        return SimpleNamespace(id=uuid.uuid4())

    async def get_stats(self, **kwargs):
        return SimpleNamespace(memory_count=1, last_active_at=datetime.now(UTC), created_at=datetime.now(UTC))

    async def delete_all_memories(self, **kwargs):
        return 1

    async def block(self, **kwargs):
        return True


class StubQualityGateService:
    async def check(self, *args, **kwargs):
        return GateResult(
            passed=True,
            blocked_layer=None,
            reason=None,
            budget_remaining_pct=0.82,
        )


class StubMappingsResult:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows


class StubExecuteResult:
    def __init__(self, rows):
        self._rows = rows

    def mappings(self):
        return StubMappingsResult(self._rows)


class StubInternalSession:
    async def execute(self, _statement):
        return StubExecuteResult([])

    async def get(self, _model, _identifier):
        return None


class StubBreaker:
    async def call(self, fn, *args, fallback=None, **kwargs):
        return await fn(*args, **kwargs)

    def call_sync(self, fn, *args, fallback=None, **kwargs):
        return fn(*args, **kwargs)

    def current_state(self):
        return "CLOSED"

    def local_state(self):
        return "CLOSED"

    def force_open(self):
        return None


class StubCircuitRegistry:
    def __init__(self) -> None:
        self.redis_cb = StubBreaker()
        self.gemini_embed_cb = StubBreaker()
        self.gemini_extract_cb = StubBreaker()
        self.qdrant_cb = StubBreaker()
        self.postgres_cb = StubBreaker()

    def get_health(self):
        return {
            "redis": "CLOSED",
            "gemini_embed": "CLOSED",
            "gemini_extract": "CLOSED",
            "qdrant": "CLOSED",
            "postgres": "CLOSED",
        }

    def overall_status(self):
        return "HEALTHY"


def _slice_with_cursor(items: list, *, cursor: str | None, limit: int):
    start_index = 0
    if cursor:
        for index, item in enumerate(items):
            if str(item.id) == cursor:
                start_index = index + 1
                break
    page = items[start_index : start_index + limit]
    next_cursor = None
    if page and (start_index + limit) < len(items):
        next_cursor = str(page[-1].id)
    return page, next_cursor, len(items)


async def bypass_auth(self, request, call_next):
    request.state.user_id = "user_abc123"
    request.state.tenant_id = "11111111-1111-1111-1111-111111111111"
    request.state.auth_scheme = "bearer"
    return await call_next(request)


def admin_headers() -> dict[str, str]:
    return {"X-Admin-Secret": os.environ["ADMIN_SECRET"]}


def build_test_client(monkeypatch, *, bypass_auth_enabled: bool) -> TestClient:
    if bypass_auth_enabled:
        monkeypatch.setattr(AuthMiddleware, "dispatch", bypass_auth)
    circuit_registry = StubCircuitRegistry()
    monkeypatch.setattr("api.main.CircuitBreakerRegistry.reset", lambda: circuit_registry)
    monkeypatch.setattr("api.main.CircuitBreakerRegistry.get_instance", lambda: circuit_registry)
    app = create_app()
    app.state.circuit_breakers = circuit_registry
    app.state.qdrant_service = object()
    app.state.cache_service = object()
    app.state.job_store = {}
    app.state.idempotency_store = {}

    memory_service = StubMemoryService()
    retriever_service = StubRetrieverService()
    user_service = StubUserService()
    api_key_service = StubApiKeyService()
    agent_service = StubAgentService()
    webhook_service = StubWebhookService()
    quality_gate_service = StubQualityGateService()
    proxy_user_service = StubProxyUserService()

    app.dependency_overrides[dependencies.get_memory_service] = lambda: memory_service
    app.dependency_overrides[dependencies.get_retriever_service] = lambda: retriever_service
    app.dependency_overrides[dependencies.get_user_service] = lambda: user_service
    app.dependency_overrides[dependencies.get_api_key_service] = lambda: api_key_service
    app.dependency_overrides[dependencies.get_agent_service] = lambda: agent_service
    app.dependency_overrides[dependencies.get_webhook_service] = lambda: webhook_service
    app.dependency_overrides[dependencies.get_quality_gate_service] = lambda: quality_gate_service
    app.dependency_overrides[dependencies.get_proxy_user_service] = lambda: proxy_user_service

    async def override_db_session():
        yield StubInternalSession()

    app.dependency_overrides[get_db_session] = override_db_session
    return TestClient(app)


def test_health_docs_idempotency_and_core_endpoints(monkeypatch) -> None:
    with build_test_client(monkeypatch, bypass_auth_enabled=True) as client:
        health_response = client.get("/health")
        docs_response = client.get("/docs")
        openapi_response = client.get("/openapi.json")
        first_add_response = client.post(
            "/v1/memories/add",
            headers={"Idempotency-Key": "idem-1"},
            json={
                "external_user_id": "ext_user_1",
                "messages": [{"role": "user", "content": "I prefer Python"}],
                "metadata": {"session_id": "sess_1"},
            },
        )
        second_add_response = client.post(
            "/v1/memories/add",
            headers={"Idempotency-Key": "idem-1"},
            json={
                "external_user_id": "ext_user_1",
                "messages": [{"role": "user", "content": "I prefer Python"}],
                "metadata": {"session_id": "sess_1"},
            },
        )
        retrieve_response = client.post(
            "/v1/memories/retrieve",
            json={
                "external_user_id": "ext_user_1",
                "query": "programming preferences",
                "limit": 10,
                "categories": ["preference"],
                "format": "bullets",
            },
        )

    assert health_response.status_code == 200
    assert health_response.json()["data"] == {
        "status": "ok",
        "qdrant": "ok",
        "postgres": "ok",
        "redis": "ok",
        "version": get_settings().app_version,
    }
    assert health_response.headers["X-MemoryOS-Quota-Mode"] == "FULL"
    assert health_response.headers["X-MemoryOS-Budget-Remaining"] == "1.0000"
    assert docs_response.status_code == 200

    paths = openapi_response.json()["paths"]
    components = openapi_response.json()["components"]
    expected_paths = {
        "/health",
        "/v1/internal/backfill-status",
        "/v1/internal/backfill/run/proxy-user-ids",
        "/v1/internal/dead-letter-jobs",
        "/v1/internal/dead-letter-jobs/{job_id}/retry",
        "/v1/internal/embedding-models/activate/{model_id}",
        "/v1/internal/queue-depth",
        "/v1/internal/reembedding-status",
        "/v1/memories",
        "/v1/memories/add",
        "/v1/memories/retrieve",
        "/v1/memories/{memory_id}",
        "/v1/memories/jobs/{job_id}",
        "/v1/users/me",
        "/v1/users/me/settings",
        "/v1/users/me/export",
        "/v1/users/{external_user_id}/stats",
        "/v1/users/{external_user_id}",
        "/v1/users/{external_user_id}/block",
        "/v1/tenant/usage",
        "/v1/tenant/cost-summary",
        "/v1/tenant/memory-additions",
        "/v1/tenant/users",
        "/v1/tenant/users/{external_user_id}/stats",
        "/v1/tenant/users/{external_user_id}",
        "/v1/tenant/users/{external_user_id}/block",
        "/v1/tenant/quality-log",
        "/v1/tenant/settings",
        "/v1/tenant/test-webhook",
        "/v1/api-keys",
        "/v1/api-keys/{api_key_id}",
        "/v1/agents",
        "/v1/webhooks/clerk",
    }
    assert expected_paths.issubset(set(paths.keys()))
    assert "BearerAuth" in components["securitySchemes"]
    assert "ApiKeyAuth" in components["securitySchemes"]
    assert paths["/v1/memories"]["get"]["security"] == [{"BearerAuth": []}, {"ApiKeyAuth": []}]
    assert paths["/v1/webhooks/clerk"]["post"]["security"] == []
    assert first_add_response.status_code == 200
    assert second_add_response.status_code == 200
    assert first_add_response.json()["job_id"] == second_add_response.json()["job_id"]
    assert retrieve_response.status_code == 200
    assert "What you know about this user:" in retrieve_response.json()["system_prompt_addition"]


def test_auth_blocks_protected_endpoints_and_allows_public_ones(monkeypatch) -> None:
    with build_test_client(monkeypatch, bypass_auth_enabled=False) as client:
        public_results = {
            "/health": client.get("/health").status_code,
            "/docs": client.get("/docs").status_code,
            "/redoc": client.get("/redoc").status_code,
            "/openapi.json": client.get("/openapi.json").status_code,
            "/v1/webhooks/clerk": client.post(
                "/v1/webhooks/clerk",
                headers={
                    "svix-id": "msg_123",
                    "svix-timestamp": "1234567890",
                    "svix-signature": "v1,test",
                },
                json={"type": "user.created", "data": {"id": "user_abc123"}},
            ).status_code,
        }
        protected_results = {
            "/v1/memories": client.get("/v1/memories").status_code,
            "/v1/memories/add": client.post(
                "/v1/memories/add",
                json={"external_user_id": "ext_user_1", "messages": [{"role": "user", "content": "hello"}]},
            ).status_code,
            "/v1/memories/retrieve": client.post(
                "/v1/memories/retrieve",
                json={"external_user_id": "ext_user_1", "query": "hello"},
            ).status_code,
            "/v1/users/me": client.get("/v1/users/me").status_code,
            "/v1/tenant/usage": client.get("/v1/tenant/usage").status_code,
            "/v1/api-keys": client.get("/v1/api-keys").status_code,
            "/v1/agents": client.get("/v1/agents").status_code,
        }

    assert all(status == 200 for status in public_results.values())
    assert all(status == 401 for status in protected_results.values())

    with build_test_client(monkeypatch, bypass_auth_enabled=False) as client:
        unauthorized = client.get("/v1/memories")
    assert unauthorized.headers["X-MemoryOS-Quota-Mode"] == "FULL"
    assert unauthorized.headers["X-MemoryOS-Budget-Remaining"] == "1.0000"


def test_list_endpoints_return_cursor_and_second_page(monkeypatch) -> None:
    with build_test_client(monkeypatch, bypass_auth_enabled=True) as client:
        memories_page_one = client.get("/v1/memories", params={"limit": 1}).json()
        memories_page_two = client.get(
            "/v1/memories",
            params={"limit": 1, "cursor": memories_page_one["pagination"]["next_cursor"]},
        ).json()

        api_keys_page_one = client.get("/v1/api-keys", params={"limit": 1}).json()
        api_keys_page_two = client.get(
            "/v1/api-keys",
            params={"limit": 1, "cursor": api_keys_page_one["pagination"]["next_cursor"]},
        ).json()

        agents_page_one = client.get("/v1/agents", params={"limit": 1}).json()
        agents_page_two = client.get(
            "/v1/agents",
            params={"limit": 1, "cursor": agents_page_one["pagination"]["next_cursor"]},
        ).json()

    assert memories_page_one["pagination"]["next_cursor"] is not None
    assert memories_page_one["data"][0]["id"] != memories_page_two["data"][0]["id"]
    assert api_keys_page_one["pagination"]["next_cursor"] is not None
    assert api_keys_page_one["data"][0]["id"] != api_keys_page_two["data"][0]["id"]
    assert agents_page_one["pagination"]["next_cursor"] is not None
    assert agents_page_one["data"][0]["id"] != agents_page_two["data"][0]["id"]


def test_remaining_endpoint_shapes(monkeypatch) -> None:
    monkeypatch.setattr(
        "api.routers.internal.run_backfill_proxy_user_ids",
        SimpleNamespace(delay=lambda **_kwargs: SimpleNamespace(id="backfill_task_123")),
    )
    async def inspect_all_queues(_self):
        return {
            "enterprise-extraction": {
                "length": 1,
                "oldest_job_age_seconds": 12,
                "tenant_breakdown": {"tenant_abc123": 1},
            }
        }

    async def activate_model(_self, model_id: str):
        return SimpleNamespace(
            id=model_id,
            provider="gemini",
            model_name="gemini-embedding-001",
            dimensions=1536,
            qdrant_collection="memories_v2",
            is_active=True,
            deprecated_at=None,
            created_at="2026-04-01T00:00:00+00:00",
        )

    monkeypatch.setattr("api.services.embedding_service.EmbeddingService.set_active_model", activate_model)
    monkeypatch.setattr("api.tasks.queue_router.QueueRouter.inspect_all_queues", inspect_all_queues)
    async def fake_get_proxy_user_detail(session, *, tenant_id: str, external_user_id: str):
        return ProxyUserDetail(
            external_user_id=external_user_id,
            user_id=external_user_id,
            memory_count=1,
            last_active_at=datetime.now(UTC),
            created_at=datetime.now(UTC),
            quality_score_avg=0.58,
            block_history=[],
            total_calls_7d=3,
            blocked_calls_7d=1,
        )

    async def fake_get_cost_summary(session, *, tenant_id: str):
        return CostSummary(
            current_month_tokens=1000,
            estimated_cost_usd=0.0002,
            cost_per_call=0.0002,
            gate_block_rate=0.25,
            projected_month_cost_usd=0.001,
            savings_from_gate_usd=0.0002,
            cost_is_estimate=True,
        )

    monkeypatch.setattr("api.routers.tenant._get_proxy_user_detail", fake_get_proxy_user_detail)
    monkeypatch.setattr("api.routers.tenant._get_cost_summary", fake_get_cost_summary)
    with build_test_client(monkeypatch, bypass_auth_enabled=True) as client:
        get_response = client.get("/v1/memories/123e4567-e89b-12d3-a456-426614174000")
        patch_response = client.patch(
            "/v1/memories/123e4567-e89b-12d3-a456-426614174000",
            json={"content": "Updated content", "importance_score": 9.0},
        )
        delete_response = client.delete("/v1/memories/123e4567-e89b-12d3-a456-426614174000")
        job_response = client.get("/v1/memories/jobs/job_123")
        profile_response = client.get("/v1/users/me")
        settings_response = client.patch(
            "/v1/users/me/settings",
            json={"settings": {"tone": "detailed"}},
        )
        export_response = client.get("/v1/users/me/export")
        delete_user_response = client.delete("/v1/users/me")
        tenant_cost_summary_response = client.get("/v1/tenant/cost-summary")
        create_key_response = client.post(
            "/v1/api-keys",
            json={"name": "SDK", "permissions": ["read"], "rate_limit_per_minute": 120},
        )
        revoke_key_response = client.delete("/v1/api-keys/123e4567-e89b-12d3-a456-426614174000")
        create_agent_response = client.post(
            "/v1/agents",
            json={"name": "assistant", "description": "Main agent", "memory_scope": "private"},
        )
        backfill_status_response = client.get("/v1/internal/backfill-status", headers=admin_headers())
        queue_depth_response = client.get("/v1/internal/queue-depth", headers=admin_headers())
        reembedding_status_response = client.get("/v1/internal/reembedding-status", headers=admin_headers())
        activate_model_response = client.post(
            "/v1/internal/embedding-models/activate/gemini-embedding-001-v2",
            headers=admin_headers(),
        )
        backfill_trigger_response = client.post(
            "/v1/internal/backfill/run/proxy-user-ids",
            params={"batch_size": 500, "sleep_between_batches_ms": 50},
            headers=admin_headers(),
        )

    assert get_response.status_code == 200
    assert patch_response.status_code == 200
    assert delete_response.status_code == 200
    assert delete_response.json()["data"]["deleted"] is True
    assert job_response.status_code == 200
    assert job_response.json()["data"]["extraction_metadata"] == {"compositional_pass_attempted": False}
    assert job_response.json()["data"]["attempts"] == 1
    assert job_response.json()["data"]["queue_name"] == "starter-extraction"
    assert profile_response.status_code == 200
    assert settings_response.status_code == 200
    assert settings_response.json()["data"] == {"settings": {"tone": "detailed"}}
    assert export_response.status_code == 200
    assert delete_user_response.status_code == 200
    assert tenant_cost_summary_response.status_code == 200
    assert tenant_cost_summary_response.json()["data"]["cost_is_estimate"] is True
    assert create_key_response.status_code == 200
    assert create_key_response.json()["data"]["raw_key"] == "mem_live_key"
    assert revoke_key_response.status_code == 200
    assert create_agent_response.status_code == 200
    assert backfill_status_response.status_code == 200
    assert queue_depth_response.status_code == 200
    assert queue_depth_response.json()["data"]["enterprise-extraction"]["length"] == 1
    assert reembedding_status_response.status_code == 200
    assert activate_model_response.status_code == 200
    assert activate_model_response.json()["data"]["id"] == "gemini-embedding-001-v2"
    assert activate_model_response.json()["data"]["is_active"] is True
    assert backfill_trigger_response.status_code == 200
    assert backfill_trigger_response.json()["data"] == {
        "task_name": "backfill_proxy_user_ids",
        "task_id": "backfill_task_123",
        "status": "queued",
        "batch_size": 500,
        "sleep_between_batches_ms": 50,
    }
