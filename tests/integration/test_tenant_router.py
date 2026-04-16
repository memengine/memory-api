from __future__ import annotations
import uuid
from datetime import UTC
from datetime import datetime
from types import SimpleNamespace

from fastapi.testclient import TestClient

from api import dependencies
from api.db.database import get_db_session
from api.db.models import CallQualityBlockedLayer
from api.db.models import OveragePolicy
from api.db.models import PlanTier
from api.db.models import QuotaMode
from api.main import create_app
from api.middleware.auth import AuthMiddleware
from api.routers import tenant as tenant_router
from api.schemas.tenant_schemas import CostSummary
from api.schemas.tenant_schemas import ProxyUserDetail
from api.schemas.tenant_schemas import TenantMemoryAdditionPoint


async def bypass_auth(self, request, call_next):
    request.state.tenant_id = "11111111-1111-1111-1111-111111111111"
    request.state.auth_scheme = "apikey"
    return await call_next(request)


class StubQuotaManager:
    async def get_mode(self, tenant_id: str):
        return QuotaMode.full

    async def get_quota_envelope(self, tenant_id: str):
        return SimpleNamespace(
            mode=QuotaMode.full,
            budget_remaining_pct=0.73,
            reset_at=datetime(2026, 4, 1, tzinfo=UTC),
        )


class StubProxyUserService:
    async def get_stats(self, **kwargs):
        return SimpleNamespace(memory_count=4, last_active_at=datetime(2026, 3, 31, tzinfo=UTC), created_at=datetime(2026, 3, 1, tzinfo=UTC))

    async def delete_all_memories(self, **kwargs):
        return 7

    async def block(self, **kwargs):
        return True


async def override_db_session():
    yield object()


def build_tenant_client(monkeypatch) -> TestClient:
    monkeypatch.setattr(AuthMiddleware, "dispatch", bypass_auth)
    app = create_app()
    app.state.cache_service = object()
    app.state.qdrant_service = object()

    app.dependency_overrides[dependencies.get_quota_manager] = lambda: StubQuotaManager()
    app.dependency_overrides[dependencies.get_proxy_user_service] = lambda: StubProxyUserService()
    app.dependency_overrides[get_db_session] = override_db_session
    return TestClient(app)


def test_tenant_usage_users_quality_log_settings_and_webhook(monkeypatch) -> None:
    tenant_budget = SimpleNamespace(
        monthly_call_limit=1000,
        current_month_calls=270,
        monthly_token_limit=50000,
        current_month_tokens=12000,
        reset_at=datetime(2026, 4, 1, tzinfo=UTC),
        plan_tier=PlanTier.starter,
        alert_webhook_url="https://tenant.example.test/webhook",
        overage_policy=OveragePolicy.warn,
    )
    proxy_users = [
        SimpleNamespace(
            id=uuid.uuid4(),
            external_user_id="user-1",
            memory_count=9,
            last_active_at=datetime(2026, 3, 31, 10, tzinfo=UTC),
            created_at=datetime(2026, 3, 1, tzinfo=UTC),
            is_blocked=False,
        ),
        SimpleNamespace(
            id=uuid.uuid4(),
            external_user_id="user-2",
            memory_count=3,
            last_active_at=datetime(2026, 3, 30, 10, tzinfo=UTC),
            created_at=datetime(2026, 3, 2, tzinfo=UTC),
            is_blocked=True,
        ),
    ]
    quality_logs = [
        SimpleNamespace(
            id=uuid.uuid4(),
            external_user_id="user-1",
            layer_blocked_at=CallQualityBlockedLayer.l2,
            quality_score=0.22,
            semantic_similarity=None,
            created_at=datetime(2026, 3, 31, 9, tzinfo=UTC),
        ),
        SimpleNamespace(
            id=uuid.uuid4(),
            external_user_id="user-2",
            layer_blocked_at=CallQualityBlockedLayer.l3,
            quality_score=0.88,
            semantic_similarity=0.97,
            created_at=datetime(2026, 3, 30, 9, tzinfo=UTC),
        ),
    ]

    async def fake_load_tenant_budget(session, tenant_id: str):
        return tenant_budget

    async def fake_list_proxy_users(session, *, tenant_id: str, cursor: str | None, limit: int):
        return [
            (proxy_users[0], 0.74),
            (proxy_users[1], None),
        ][:limit], "next-user-cursor", len(proxy_users)

    async def fake_list_quality_logs(session, *, tenant_id: str, cursor: str | None, limit: int):
        return quality_logs[:limit], "next-log-cursor", len(quality_logs)

    async def fake_update_tenant_budget_settings(session, *, tenant_id: str, payload):
        tenant_budget.alert_webhook_url = payload.alert_webhook_url
        tenant_budget.overage_policy = OveragePolicy(payload.overage_policy)
        return tenant_budget

    async def fake_send_test_webhook(webhook_url: str, tenant_id: str):
        return True, 202

    async def fake_get_proxy_user_detail(session, *, tenant_id: str, external_user_id: str):
        return ProxyUserDetail(
            external_user_id=external_user_id,
            user_id=external_user_id,
            memory_count=4,
            last_active_at=datetime(2026, 3, 31, tzinfo=UTC),
            created_at=datetime(2026, 3, 1, tzinfo=UTC),
            quality_score_avg=0.63,
            block_history=[
                {
                    "blocked_at": datetime(2026, 3, 31, 8, tzinfo=UTC),
                    "layer": "L2",
                    "reason": "low_quality",
                }
            ],
            total_calls_7d=12,
            blocked_calls_7d=2,
        )

    async def fake_get_cost_summary(session, *, tenant_id: str):
        return CostSummary(
            current_month_tokens=12000,
            estimated_cost_usd=0.0018,
            cost_per_call=0.000007,
            gate_block_rate=0.1667,
            projected_month_cost_usd=0.0054,
            savings_from_gate_usd=0.0001,
            cost_is_estimate=True,
        )

    async def fake_get_tenant_memory_additions(session, *, tenant_id: str, limit: int):
        return [
            TenantMemoryAdditionPoint(day=datetime(2026, 3, 29, tzinfo=UTC), count=2),
            TenantMemoryAdditionPoint(day=datetime(2026, 3, 30, tzinfo=UTC), count=5),
        ][:limit]

    monkeypatch.setattr(tenant_router, "_load_tenant_budget", fake_load_tenant_budget)
    monkeypatch.setattr(tenant_router, "_list_proxy_users", fake_list_proxy_users)
    monkeypatch.setattr(tenant_router, "_list_quality_logs", fake_list_quality_logs)
    monkeypatch.setattr(tenant_router, "_update_tenant_budget_settings", fake_update_tenant_budget_settings)
    monkeypatch.setattr(tenant_router, "_send_test_webhook", fake_send_test_webhook)
    monkeypatch.setattr(tenant_router, "_get_proxy_user_detail", fake_get_proxy_user_detail)
    monkeypatch.setattr(tenant_router, "_get_cost_summary", fake_get_cost_summary)
    monkeypatch.setattr(tenant_router, "_get_tenant_memory_additions", fake_get_tenant_memory_additions)

    with build_tenant_client(monkeypatch) as client:
        usage_response = client.get("/v1/tenant/usage")
        users_response = client.get("/v1/tenant/users", params={"limit": 2})
        stats_response = client.get("/v1/tenant/users/ext-user-123/stats")
        cost_summary_response = client.get("/v1/tenant/cost-summary")
        additions_response = client.get("/v1/tenant/memory-additions", params={"limit": 7})
        quality_response = client.get("/v1/tenant/quality-log", params={"limit": 2})
        settings_response = client.patch(
            "/v1/tenant/settings",
            json={"alert_webhook_url": "https://tenant.example.test/new", "overage_policy": "charge"},
        )
        webhook_response = client.post("/v1/tenant/test-webhook")

    assert usage_response.status_code == 200
    assert usage_response.json()["data"]["mode"] == "FULL"
    assert usage_response.json()["data"]["budget_remaining_pct"] == 0.73

    assert users_response.status_code == 200
    assert users_response.json()["pagination"]["next_cursor"] == "next-user-cursor"
    assert users_response.json()["data"][0]["external_user_id"] == "user-1"
    assert users_response.json()["data"][0]["quality_score_avg"] == 0.74

    assert stats_response.status_code == 200
    assert stats_response.json()["data"]["external_user_id"] == "ext-user-123"
    assert stats_response.json()["data"]["user_id"] == "ext-user-123"
    assert stats_response.json()["data"]["memory_count"] == 4
    assert stats_response.json()["data"]["quality_score_avg"] == 0.63
    assert stats_response.json()["data"]["total_calls_7d"] == 12
    assert stats_response.json()["data"]["blocked_calls_7d"] == 2
    assert stats_response.json()["data"]["block_history"][0]["layer"] == "L2"
    assert stats_response.json()["data"]["block_history"][0]["reason"] == "low_quality"
    assert stats_response.headers["Deprecation"] == "true"
    assert "2026-10-01" in stats_response.headers["Sunset"]
    assert "user_id (sunset: 2026-10-01)" in stats_response.headers["X-MemoryOS-Deprecated-Fields"]

    assert cost_summary_response.status_code == 200
    assert cost_summary_response.json()["data"]["current_month_tokens"] == 12000
    assert cost_summary_response.json()["data"]["cost_is_estimate"] is True

    assert additions_response.status_code == 200
    assert additions_response.json()["data"][0]["count"] == 2
    assert additions_response.json()["data"][1]["count"] == 5

    assert quality_response.status_code == 200
    assert quality_response.json()["pagination"]["next_cursor"] == "next-log-cursor"
    assert quality_response.json()["data"][0]["layer_blocked_at"] == "L2"

    assert settings_response.status_code == 200
    assert settings_response.json()["data"] == {
        "alert_webhook_url": "https://tenant.example.test/new",
        "overage_policy": "charge",
    }

    assert webhook_response.status_code == 200
    assert webhook_response.json()["data"] == {"delivered": True, "status_code": 202}


def test_tenant_proxy_user_delete_and_block(monkeypatch) -> None:
    async def fake_load_tenant_budget(session, tenant_id: str):
        return SimpleNamespace(
            monthly_call_limit=1000,
            current_month_calls=10,
            monthly_token_limit=50000,
            current_month_tokens=1000,
            reset_at=datetime(2026, 4, 1, tzinfo=UTC),
            plan_tier=PlanTier.starter,
            alert_webhook_url="https://tenant.example.test/webhook",
            overage_policy=OveragePolicy.warn,
        )

    monkeypatch.setattr(tenant_router, "_load_tenant_budget", fake_load_tenant_budget)

    with build_tenant_client(monkeypatch) as client:
        delete_response = client.delete("/v1/tenant/users/ext-user-123")
        block_response = client.post("/v1/tenant/users/ext-user-123/block")

    assert delete_response.status_code == 200
    assert delete_response.json()["data"] == {"deleted": True, "memories_removed": 7}
    assert block_response.status_code == 200
    assert block_response.json()["data"] == {"blocked": True}
