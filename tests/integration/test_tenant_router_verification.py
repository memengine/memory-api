from __future__ import annotations

import uuid
from datetime import UTC
from datetime import datetime
from types import SimpleNamespace

from fastapi.testclient import TestClient
from qdrant_client.http import models as qmodels
from sqlalchemy import create_engine
from sqlalchemy import delete
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from api import dependencies
from api.db.database import get_sync_database_url
from api.db.models import Conversation
from api.db.models import EmbeddingModel
from api.db.models import Memory
from api.db.models import MemoryCategory
from api.db.models import OveragePolicy
from api.db.models import PlanTier
from api.db.models import ProxyUser
from api.db.models import Tenant
from api.db.models import TenantBudget
from api.db.models import User
from api.db.vector_store import QdrantService
from api.infra.circuit_breaker_registry import CircuitBreakerRegistry
from api.main import create_app
from api.middleware.auth import AuthMiddleware
from api.routers import tenant as tenant_router
from api.services.proxy_user_service import ProxyUserService
from api.services.quality_gate import GateResult
from api.tasks.vector_sync_tasks import run_outbox_cycle


SYNC_ENGINE = create_engine(get_sync_database_url(), pool_pre_ping=True)
SyncSessionLocal = sessionmaker(bind=SYNC_ENGINE, expire_on_commit=False)


class AsyncSessionAdapter:
    def __init__(self) -> None:
        self._session = SyncSessionLocal()

    def add(self, instance) -> None:
        self._session.add(instance)

    async def delete(self, instance) -> None:
        self._session.delete(instance)

    async def get(self, model, ident):
        return self._session.get(model, ident)

    async def execute(self, statement):
        return self._session.execute(statement)

    async def commit(self) -> None:
        self._session.commit()

    async def refresh(self, instance) -> None:
        self._session.refresh(instance)

    async def close(self) -> None:
        self._session.close()


class InMemoryQdrantClient:
    def __init__(self) -> None:
        self.points: dict[str, dict[str, object]] = {}

    def upsert(self, *, collection_name: str, points: list[qmodels.PointStruct], wait: bool = True):
        for point in points:
            self.points[str(point.id)] = {
                "collection_name": collection_name,
                "payload": dict(point.payload or {}),
                "vector": list(point.vector or []),
            }
        return True

    def delete(
        self,
        *,
        collection_name: str,
        points_selector,
        wait: bool = True,
    ):
        if isinstance(points_selector, qmodels.PointIdsList):
            for point_id in points_selector.points:
                self.points.pop(str(point_id), None)
            return True

        if isinstance(points_selector, qmodels.FilterSelector):
            conditions = getattr(points_selector.filter, "must", []) or []
            to_delete = [
                point_id
                for point_id, entry in self.points.items()
                if entry["collection_name"] == collection_name and _payload_matches(conditions, entry["payload"])
            ]
            for point_id in to_delete:
                self.points.pop(point_id, None)
            return True

        raise TypeError(f"Unsupported points_selector: {type(points_selector)!r}")

    def count(self, *, collection_name: str, count_filter, exact: bool = True):
        conditions = getattr(count_filter, "must", []) or []
        count = sum(
            1
            for entry in self.points.values()
            if entry["collection_name"] == collection_name and _payload_matches(conditions, entry["payload"])
        )
        return SimpleNamespace(count=count)


class InMemoryQdrantService:
    COLLECTION_NAME = "memories"
    VECTOR_SIZE = 1536

    def __init__(self) -> None:
        self.client = InMemoryQdrantClient()
        self.breaker = SimpleNamespace(call_sync=lambda fn, *args, fallback=None, **kwargs: fn(*args, **kwargs))

    def _ensure_collection_if_possible(self, collection_name: str, vector_size: int) -> None:
        return None


def _payload_matches(conditions, payload: dict[str, object]) -> bool:
    for condition in conditions:
        key = getattr(condition, "key", None)
        match = getattr(condition, "match", None)
        expected = getattr(match, "value", None)
        if payload.get(str(key)) != expected:
            return False
    return True


def _messages() -> list[dict[str, str]]:
    return [
        {"role": "user", "content": "I am building a B2B AI memory platform."},
        {"role": "assistant", "content": "What do you need help with?"},
        {"role": "user", "content": "Quota controls, tenant isolation, and auditability."},
    ]


def _build_auth_bypass(tenant_id: str):
    async def bypass_auth(self, request, call_next):
        request.state.tenant_id = tenant_id
        request.state.user_id = None
        request.state.auth_scheme = "apikey"
        return await call_next(request)

    return bypass_auth


class StubQueueMemoryService:
    async def queue_memory_add(self, **kwargs):
        return {"job_id": f"job_{uuid.uuid4().hex[:8]}", "status": "queued"}


class CountingGateService:
    def __init__(self, tenant_id: str) -> None:
        self.tenant_id = tenant_id

    async def check(self, messages, tenant_id, external_user_id):
        assert tenant_id == self.tenant_id
        with SyncSessionLocal() as session:
            result = session.execute(
                select(TenantBudget).where(TenantBudget.tenant_id == uuid.UUID(tenant_id))
            )
            tenant_budget = result.scalar_one()
            tenant_budget.current_month_calls = int(tenant_budget.current_month_calls or 0) + 1
            tenant_budget.current_month_tokens = int(tenant_budget.current_month_tokens or 0) + 25
            session.commit()
            remaining_pct = max(
                0.0,
                round(
                    1.0
                    - (
                        int(tenant_budget.current_month_calls)
                        / int(tenant_budget.monthly_call_limit or 1)
                    ),
                    4,
                ),
            )
        return GateResult(
            passed=True,
            blocked_layer=None,
            reason=None,
            budget_remaining_pct=remaining_pct,
        )


def _create_tenant_with_budget(
    *,
    tenant_id: str,
    company_name: str,
    monthly_call_limit: int = 1000,
    alert_webhook_url: str | None = None,
) -> None:
    with SyncSessionLocal() as session:
        session.add(
            Tenant(
                id=uuid.UUID(tenant_id),
                company_name=company_name,
                plan_tier=PlanTier.starter,
            )
        )
        session.add(
            TenantBudget(
                tenant_id=uuid.UUID(tenant_id),
                plan_tier=PlanTier.starter,
                monthly_call_limit=monthly_call_limit,
                monthly_token_limit=100_000,
                current_month_calls=0,
                current_month_tokens=0,
                overage_policy=OveragePolicy.warn,
                alert_webhook_url=alert_webhook_url,
            )
        )
        session.commit()


def _create_proxy_user(
    *,
    tenant_id: str,
    external_user_id: str,
    memory_count: int = 0,
) -> ProxyUser:
    with SyncSessionLocal() as session:
        proxy_user = ProxyUser(
            tenant_id=uuid.UUID(tenant_id),
            external_user_id=external_user_id,
            external_user_id_hash=ProxyUserService.hash_external_user_id(tenant_id, external_user_id),
            memory_count=memory_count,
        )
        session.add(proxy_user)
        session.commit()
        session.refresh(proxy_user)
        return proxy_user


def _cleanup_tenants(*tenant_ids: str) -> None:
    tenant_uuids = [uuid.UUID(value) for value in tenant_ids]
    with SyncSessionLocal() as session:
        proxy_ids = list(
            session.execute(
                select(ProxyUser.id).where(ProxyUser.tenant_id.in_(tenant_uuids))
            ).scalars()
        )
        user_ids = list(
            session.execute(
                select(User.id).where(User.external_id.like("tenant-test-%"))
            ).scalars()
        )
        if proxy_ids:
            session.execute(delete(Memory).where(Memory.proxy_user_id.in_(proxy_ids)))
            session.execute(delete(ProxyUser).where(ProxyUser.id.in_(proxy_ids)))
        if user_ids:
            session.execute(delete(Conversation).where(Conversation.user_id.in_(user_ids)))
            session.execute(delete(User).where(User.id.in_(user_ids)))
        session.execute(delete(TenantBudget).where(TenantBudget.tenant_id.in_(tenant_uuids)))
        session.execute(delete(Tenant).where(Tenant.id.in_(tenant_uuids)))
        session.commit()


def _seed_memory_with_vector(
    *,
    tenant_id: str,
    external_user_id: str,
    qdrant_service: InMemoryQdrantService | None = None,
) -> tuple[str, str, str]:
    with SyncSessionLocal() as session:
        active_embedding_model_id = session.scalar(
            select(EmbeddingModel.id).where(EmbeddingModel.is_active.is_(True)).limit(1)
        )
        proxy_user = ProxyUser(
            tenant_id=uuid.UUID(tenant_id),
            external_user_id=external_user_id,
            external_user_id_hash=ProxyUserService.hash_external_user_id(tenant_id, external_user_id),
            memory_count=1,
        )
        user = User(
            external_id=f"tenant-test-{uuid.uuid4()}",
            email=f"tenant-test-{uuid.uuid4()}@example.com",
            settings={},
        )
        session.add_all([proxy_user, user])
        session.flush()

        conversation = Conversation(
            user_id=user.id,
            agent_id=None,
            message_count=2,
        )
        session.add(conversation)
        session.flush()

        memory = Memory(
            user_id=user.id,
            proxy_user_id=proxy_user.id,
            agent_id=None,
            content="Tenant-scoped memory for GDPR delete verification",
            category=MemoryCategory.fact,
            importance_score=6.5,
            confidence_score=0.9,
            embedding_id=str(uuid.uuid4()),
            embedding_model_id=str(active_embedding_model_id),
            source_conversation_id=conversation.id,
            metadata_json={"tenant_test": True},
        )
        session.add(memory)
        session.commit()
        session.refresh(proxy_user)
        session.refresh(memory)

        qdrant = qdrant_service or QdrantService()
        qdrant._ensure_collection_if_possible(qdrant.COLLECTION_NAME, qdrant.VECTOR_SIZE)
        qdrant.client.upsert(
            collection_name=qdrant.COLLECTION_NAME,
            points=[
                qmodels.PointStruct(
                    id=str(memory.id),
                    vector=[0.01] * qdrant.VECTOR_SIZE,
                    payload={
                        "tenant_id": tenant_id,
                        "proxy_user_id": str(proxy_user.id),
                        "category": memory.category.value,
                        "importance_score": float(memory.importance_score),
                        "is_archived": False,
                        "created_at": memory.created_at.isoformat(),
                    },
                )
            ],
            wait=True,
        )
        return str(proxy_user.id), str(memory.id), str(user.id)


def _build_client(monkeypatch, *, tenant_id: str, overrides: dict | None = None) -> TestClient:
    monkeypatch.setattr(AuthMiddleware, "dispatch", _build_auth_bypass(tenant_id))
    app = create_app()
    app.state.circuit_breakers = CircuitBreakerRegistry.reset()
    async def override_db_session():
        session = AsyncSessionAdapter()
        try:
            yield session
        finally:
            await session.close()

    app.dependency_overrides[dependencies.get_db_session] = override_db_session
    if overrides:
        app.dependency_overrides.update(overrides)
    return TestClient(app)


def test_tenant_usage_reflects_ten_add_calls(monkeypatch) -> None:
    tenant_id = str(uuid.uuid4())
    _create_tenant_with_budget(tenant_id=tenant_id, company_name="Usage Tenant")
    overrides = {
        dependencies.get_memory_service: lambda: StubQueueMemoryService(),
        dependencies.get_quality_gate_service: lambda: CountingGateService(tenant_id),
    }

    try:
        with _build_client(monkeypatch, tenant_id=tenant_id, overrides=overrides) as client:
            for _ in range(10):
                response = client.post(
                    "/v1/memories/add",
                    json={
                        "external_user_id": "usage-user-1",
                        "messages": _messages(),
                        "metadata": {"source": "tenant-usage-test"},
                    },
                )
                assert response.status_code == 200
                assert response.json()["status"] == "queued"

            usage_response = client.get("/v1/tenant/usage")

        assert usage_response.status_code == 200
        assert usage_response.json()["data"]["calls_used"] == 10
    finally:
        _cleanup_tenants(tenant_id)


def test_tenant_users_are_scoped_and_cross_tenant_stats_return_404(monkeypatch) -> None:
    tenant_a = str(uuid.uuid4())
    tenant_b = str(uuid.uuid4())
    _create_tenant_with_budget(tenant_id=tenant_a, company_name="Tenant A")
    _create_tenant_with_budget(tenant_id=tenant_b, company_name="Tenant B")
    _create_proxy_user(tenant_id=tenant_a, external_user_id="tenant-a-user", memory_count=2)
    _create_proxy_user(tenant_id=tenant_b, external_user_id="tenant-b-user", memory_count=5)

    try:
        with _build_client(monkeypatch, tenant_id=tenant_a) as client:
            list_response = client.get("/v1/tenant/users")
            cross_tenant_stats = client.get("/v1/tenant/users/tenant-b-user/stats")

        assert list_response.status_code == 200
        external_ids = [item["external_user_id"] for item in list_response.json()["data"]]
        assert "tenant-a-user" in external_ids
        assert "tenant-b-user" not in external_ids

        assert cross_tenant_stats.status_code == 404
        assert cross_tenant_stats.json()["error"] == "proxy_user_not_found"
    finally:
        _cleanup_tenants(tenant_a, tenant_b)


def test_tenant_delete_removes_memories_from_postgres_and_qdrant(monkeypatch) -> None:
    tenant_id = str(uuid.uuid4())
    external_user_id = f"gdpr-user-{uuid.uuid4().hex[:8]}"
    _create_tenant_with_budget(tenant_id=tenant_id, company_name="GDPR Tenant")
    qdrant = InMemoryQdrantService()
    proxy_user_id, memory_id, _user_id = _seed_memory_with_vector(
        tenant_id=tenant_id,
        external_user_id=external_user_id,
        qdrant_service=qdrant,
    )

    try:
        with _build_client(monkeypatch, tenant_id=tenant_id) as client:
            delete_response = client.delete(f"/v1/tenant/users/{external_user_id}")

        assert delete_response.status_code == 200
        assert delete_response.json()["data"]["deleted"] is True

        with SyncSessionLocal() as session:
            memory_row = session.scalar(
                select(Memory.id).where(Memory.proxy_user_id == uuid.UUID(proxy_user_id)).limit(1)
            )
            proxy_user_row = session.scalar(
                select(ProxyUser.id).where(ProxyUser.id == uuid.UUID(proxy_user_id))
            )
        assert memory_row is None
        assert proxy_user_row is None

        run_outbox_cycle(qdrant_service=qdrant)

        count_result = qdrant.client.count(
            collection_name=qdrant.COLLECTION_NAME,
            count_filter=qmodels.Filter(
                must=[
                    qmodels.FieldCondition(
                        key="tenant_id",
                        match=qmodels.MatchValue(value=tenant_id),
                    ),
                    qmodels.FieldCondition(
                        key="proxy_user_id",
                        match=qmodels.MatchValue(value=proxy_user_id),
                    ),
                ]
            ),
            exact=True,
        )
        assert int(count_result.count) == 0
    finally:
        qdrant.client.delete(
            collection_name=qdrant.COLLECTION_NAME,
            points_selector=qmodels.PointIdsList(points=[str(memory_id)]),
            wait=True,
        )
        _cleanup_tenants(tenant_id)


def test_tenant_test_webhook_success_and_unreachable(monkeypatch) -> None:
    tenant_id = str(uuid.uuid4())
    _create_tenant_with_budget(
        tenant_id=tenant_id,
        company_name="Webhook Tenant",
        alert_webhook_url="https://tenant.example.test/webhook",
    )

    try:
        async def success_send_test_webhook(webhook_url: str, request_tenant_id: str):
            assert request_tenant_id == tenant_id
            return True, 200

        async def failing_send_test_webhook(webhook_url: str, request_tenant_id: str):
            assert request_tenant_id == tenant_id
            return False, 0

        monkeypatch.setattr(tenant_router, "_send_test_webhook", success_send_test_webhook)
        with _build_client(monkeypatch, tenant_id=tenant_id) as client:
            success_response = client.post("/v1/tenant/test-webhook")

        assert success_response.status_code == 200
        assert success_response.json()["data"] == {"delivered": True, "status_code": 200}

        monkeypatch.setattr(tenant_router, "_send_test_webhook", failing_send_test_webhook)
        with _build_client(monkeypatch, tenant_id=tenant_id) as client:
            failure_response = client.post("/v1/tenant/test-webhook")

        assert failure_response.status_code == 200
        assert failure_response.json()["data"]["delivered"] is False
        assert failure_response.json()["data"]["status_code"] == 0
    finally:
        _cleanup_tenants(tenant_id)
