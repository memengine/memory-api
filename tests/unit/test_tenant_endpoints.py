from __future__ import annotations

from datetime import UTC
from datetime import datetime

from fastapi.testclient import TestClient

from api import dependencies
from api.db.database import get_db_session
from api.main import create_app
from api.middleware.auth import AuthMiddleware
from api.routers import tenant as tenant_router
from api.schemas.tenant_schemas import CostSummary
from api.schemas.tenant_schemas import ProxyUserDetail


async def bypass_auth(self, request, call_next):
    request.state.tenant_id = "11111111-1111-1111-1111-111111111111"
    request.state.auth_scheme = "apikey"
    return await call_next(request)


async def override_db_session():
    yield object()


def build_tenant_client(monkeypatch) -> TestClient:
    monkeypatch.setattr(AuthMiddleware, "dispatch", bypass_auth)
    app = create_app()
    app.state.cache_service = object()
    app.state.qdrant_service = object()
    app.dependency_overrides[get_db_session] = override_db_session
    return TestClient(app)


def test_tenant_users_include_quality_score_avg_and_cost_summary(monkeypatch) -> None:
    async def fake_list_proxy_users(session, *, tenant_id: str, cursor: str | None, limit: int):
        user = type(
            "ProxyUserStub",
            (),
            {
                "external_user_id": "user-1",
                "memory_count": 3,
                "last_active_at": datetime(2026, 4, 4, tzinfo=UTC),
                "created_at": datetime(2026, 4, 1, tzinfo=UTC),
                "is_blocked": False,
            },
        )()
        return [(user, 0.71)], None, 1

    async def fake_get_proxy_user_detail(session, *, tenant_id: str, external_user_id: str):
        return ProxyUserDetail(
            external_user_id=external_user_id,
            user_id=external_user_id,
            memory_count=3,
            last_active_at=datetime(2026, 4, 4, tzinfo=UTC),
            created_at=datetime(2026, 4, 1, tzinfo=UTC),
            quality_score_avg=0.71,
            block_history=[],
            total_calls_7d=9,
            blocked_calls_7d=1,
        )

    async def fake_get_cost_summary(session, *, tenant_id: str):
        return CostSummary(
            current_month_tokens=12000,
            estimated_cost_usd=0.0018,
            cost_per_call=0.0002,
            gate_block_rate=0.1111,
            projected_month_cost_usd=0.0135,
            savings_from_gate_usd=0.0002,
            cost_is_estimate=True,
        )

    monkeypatch.setattr(tenant_router, "_list_proxy_users", fake_list_proxy_users)
    monkeypatch.setattr(tenant_router, "_get_proxy_user_detail", fake_get_proxy_user_detail)
    monkeypatch.setattr(tenant_router, "_get_cost_summary", fake_get_cost_summary)

    with build_tenant_client(monkeypatch) as client:
        users_response = client.get("/v1/tenant/users")
        stats_response = client.get("/v1/tenant/users/user-1/stats")
        cost_response = client.get("/v1/tenant/cost-summary")

    assert users_response.status_code == 200
    assert users_response.json()["data"][0]["quality_score_avg"] == 0.71

    assert stats_response.status_code == 200
    assert stats_response.json()["data"]["quality_score_avg"] == 0.71
    assert stats_response.json()["data"]["total_calls_7d"] == 9
    assert stats_response.json()["data"]["blocked_calls_7d"] == 1

    assert cost_response.status_code == 200
    assert cost_response.json()["data"] == {
        "current_month_tokens": 12000,
        "estimated_cost_usd": 0.0018,
        "cost_per_call": 0.0002,
        "gate_block_rate": 0.1111,
        "projected_month_cost_usd": 0.0135,
        "savings_from_gate_usd": 0.0002,
        "cost_is_estimate": True,
    }
