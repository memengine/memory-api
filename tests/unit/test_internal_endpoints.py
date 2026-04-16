from __future__ import annotations

import json
import os
import uuid
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from types import SimpleNamespace

import fakeredis.aioredis
import pytest
from fastapi.testclient import TestClient

from api.db.cache import CacheService
from api.db.models import AuditAction
from api.db.models import AuditLog
from api.db.models import ExtractionJobStatus
from api.main import create_app
from api.routers.internal import _discard_dead_letter_job
from api.routers.internal import _reset_circuit_breaker
from api.routers.internal import _get_system_health
from api.schemas.internal_schemas import AllTenantsResponse
from api.schemas.internal_schemas import CircuitStatus
from api.schemas.internal_schemas import CostSummaryTenant
from api.schemas.internal_schemas import InternalTenantRecord
from api.schemas.internal_schemas import QualitySummary
from api.schemas.internal_schemas import SystemCostSummary
from api.schemas.internal_schemas import TenantDetail
from api.schemas.internal_schemas import TenantSummary
from api.schemas.tenant_schemas import TenantUsageData


VALID_ADMIN_SECRET = os.environ["ADMIN_SECRET"]


class FakeDiscardSession:
    def __init__(self, job) -> None:
        self.job = job
        self.added: list[object] = []
        self.deleted: list[object] = []
        self.committed = False

    async def get(self, _model, identifier):
        if str(identifier) == str(self.job.id):
            return self.job
        return None

    def add(self, value) -> None:
        self.added.append(value)

    async def delete(self, value) -> None:
        self.deleted.append(value)

    async def commit(self) -> None:
        self.committed = True


class FakeCircuitBreaker:
    def __init__(self) -> None:
        self.reset_calls = 0

    def _record_success(self) -> None:
        self.reset_calls += 1

    def snapshot(self) -> dict[str, object]:
        return {
            "state": "CLOSED",
            "failure_count": 0,
            "window_started_at": 0.0,
            "opened_at": 0.0,
        }


@pytest.mark.asyncio
async def test_system_health_uses_cached_redis_state_only() -> None:
    cache_service = CacheService(client=fakeredis.aioredis.FakeRedis(decode_responses=True))
    now = datetime.now(UTC)
    await cache_service.client.set(
        "cb:postgres:state",
        json.dumps(
            {
                "state": "OPEN",
                "failure_count": 4,
                "opened_at": now.timestamp(),
            }
        ),
    )
    await cache_service.client.set(
        "cb:gemini_embed:state",
        json.dumps(
            {
                "state": "HALF_OPEN",
                "failure_count": 2,
                "opened_at": (now - timedelta(seconds=30)).timestamp(),
            }
        ),
    )
    await cache_service.client.set("queue_depth:enterprise-extraction", "250")
    await cache_service.client.zadd(
        "queue_depth:enterprise-extraction:jobs",
        {"tenant-a:job-1": (now - timedelta(seconds=45)).timestamp()},
    )

    response = await _get_system_health(cache_service)

    assert response.overall_status == "CRITICAL"
    assert any(item.name == "postgres" and item.state == "OPEN" for item in response.circuits)
    enterprise_queue = next(item for item in response.queues if item.name == "enterprise-extraction")
    assert enterprise_queue.depth == 250
    assert enterprise_queue.status == "CRITICAL"
    assert enterprise_queue.oldest_job_age_seconds is not None


@pytest.mark.asyncio
async def test_discard_dead_letter_job_writes_audit_log() -> None:
    job = SimpleNamespace(
        id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        proxy_user_id=uuid.uuid4(),
        status=ExtractionJobStatus.dead,
    )
    session = FakeDiscardSession(job)
    request = SimpleNamespace(client=SimpleNamespace(host="127.0.0.1"))

    response = await _discard_dead_letter_job(session, request=request, job_id=str(job.id))

    assert response.discarded is True
    assert response.job_id == str(job.id)
    assert session.deleted == [job]
    assert session.committed is True
    assert any(isinstance(item, AuditLog) and item.action == AuditAction.job_discarded for item in session.added)


@pytest.mark.asyncio
async def test_reset_circuit_breaker_closes_state_and_updates_cache() -> None:
    cache_service = CacheService(client=fakeredis.aioredis.FakeRedis(decode_responses=True))
    breaker = FakeCircuitBreaker()
    request = SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(circuit_breakers=SimpleNamespace(_breakers={"postgres": breaker}))
        )
    )

    response = await _reset_circuit_breaker(
        request=request,
        cache_service=cache_service,
        circuit_name="postgres",
    )

    assert response.state == "CLOSED"
    assert response.failure_count == 0
    assert breaker.reset_calls == 1
    cached = await cache_service.client.get("cb:postgres:state")
    assert cached is not None
    payload = json.loads(cached)
    assert payload["state"] == "CLOSED"
    assert payload["failure_count"] == 0


def test_all_tenants_endpoint_returns_operator_summary(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_all_tenants(*args, **kwargs):
        return AllTenantsResponse(
            tenants=[
                TenantSummary(
                    tenant_id=str(uuid.uuid4()),
                    company_name="Acme",
                    plan_tier="growth",
                    quota_mode="FULL",
                    quota_pct=0.42,
                    memory_count=120,
                    active_users_7d=14,
                    dead_job_count=0,
                    last_api_call=datetime.now(UTC),
                    needs_attention=False,
                )
            ],
            next_cursor="next-cursor",
            limit=50,
            generated_at=datetime.now(UTC),
        )

    monkeypatch.setattr(
        "api.routers.internal._list_all_tenants",
        fake_all_tenants,
    )
    app = create_app()

    with TestClient(app) as client:
        response = client.get("/v1/internal/all-tenants", headers={"X-Admin-Secret": VALID_ADMIN_SECRET})

    assert response.status_code == 200
    payload = response.json()
    assert payload["tenants"][0]["company_name"] == "Acme"
    assert payload["tenants"][0]["quota_mode"] == "FULL"
    assert payload["next_cursor"] == "next-cursor"


def test_reset_circuit_endpoint_returns_closed_state(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_reset(*args, **kwargs):
        return CircuitStatus(
            name=kwargs["circuit_name"],
            state="CLOSED",
            open_since=None,
            failure_count=0,
        )

    monkeypatch.setattr(
        "api.routers.internal._reset_circuit_breaker",
        fake_reset,
    )
    app = create_app()

    with TestClient(app) as client:
        response = client.post(
            "/v1/internal/circuit/postgres/reset",
            headers={"X-Admin-Secret": VALID_ADMIN_SECRET},
        )

    assert response.status_code == 200
    assert response.json()["name"] == "postgres"
    assert response.json()["state"] == "CLOSED"


def test_internal_tenant_detail_endpoint_returns_deep_dive(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_tenant_detail(*args, **kwargs):
        return TenantDetail(
            tenant=InternalTenantRecord(
                tenant_id=str(uuid.uuid4()),
                company_name="Acme",
                plan_tier="starter",
                created_at=datetime.now(UTC),
            ),
            usage=TenantUsageData(
                calls_used=12,
                calls_limit=1000,
                tokens_used=3456,
                tokens_limit=100000,
                mode="FULL",
                budget_remaining_pct=0.88,
                reset_at=datetime.now(UTC),
                plan_tier="starter",
            ),
            recent_jobs=[],
            quality_summary=QualitySummary(
                total_calls=10,
                blocked_calls=2,
                block_rate=0.2,
                by_layer={"L1": 1, "L2": 1, "L3": 0, "L4": 0},
            ),
            cost_estimate_mtd=0.0012,
            cost_is_estimate=True,
        )

    monkeypatch.setattr(
        "api.routers.internal._get_internal_tenant_detail",
        fake_tenant_detail,
    )
    app = create_app()

    with TestClient(app) as client:
        response = client.get(
            f"/v1/internal/tenant/{uuid.uuid4()}",
            headers={"X-Admin-Secret": VALID_ADMIN_SECRET},
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["tenant"]["company_name"] == "Acme"
    assert payload["usage"]["mode"] == "FULL"
    assert payload["quality_summary"]["blocked_calls"] == 2


def test_internal_cost_summary_endpoint_returns_estimate(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_cost_summary(*_args, **_kwargs):
        return SystemCostSummary(
            total_tokens_mtd=1250000,
            total_estimated_cost_usd=0.1875,
            top_5_tenants_by_cost=[
                CostSummaryTenant(
                    tenant_id=str(uuid.uuid4()),
                    company_name="Acme",
                    tokens=600000,
                    estimated_cost_usd=0.09,
                )
            ],
            avg_cost_per_call=0.000321,
            total_gate_blocks_mtd=12,
            estimated_savings_from_gate_usd=0.0039,
            projected_month_cost_usd=0.5625,
            cost_is_estimate=True,
        )

    monkeypatch.setattr(
        "api.routers.internal._get_system_cost_summary",
        fake_cost_summary,
    )
    app = create_app()

    with TestClient(app) as client:
        response = client.get("/v1/internal/cost-summary", headers={"X-Admin-Secret": VALID_ADMIN_SECRET})

    assert response.status_code == 200
    payload = response.json()
    assert payload["cost_is_estimate"] is True
    assert payload["top_5_tenants_by_cost"][0]["company_name"] == "Acme"


def test_dead_letter_delete_endpoint_uses_admin_secret_only(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_discard(*args, **kwargs):
        return {"discarded": True, "job_id": str(kwargs["job_id"])}

    monkeypatch.setattr(
        "api.routers.internal._discard_dead_letter_job",
        fake_discard,
    )
    app = create_app()
    job_id = str(uuid.uuid4())

    with TestClient(app) as client:
        response = client.delete(
            f"/v1/internal/dead-letter-jobs/{job_id}",
            headers={"X-Admin-Secret": VALID_ADMIN_SECRET},
        )

    assert response.status_code == 200
    assert response.json() == {"discarded": True, "job_id": job_id}
