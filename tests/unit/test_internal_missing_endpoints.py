from __future__ import annotations

import os
import uuid
from datetime import UTC
from datetime import datetime
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from api.main import create_app
from api.routers.internal import _get_system_cost_summary
from api.routers.internal import _list_backfill_jobs
from api.schemas.internal_schemas import AuditLogsResponse
from api.schemas.internal_schemas import BackfillJobResponse
from api.schemas.internal_schemas import CostSummaryResponse


VALID_ADMIN_SECRET = os.environ["ADMIN_SECRET"]


class FakeResult:
    def __init__(self, *, one_value=None, all_value=None, mappings_value=None, scalar_value=None) -> None:
        self._one_value = one_value
        self._all_value = all_value or []
        self._mappings_value = mappings_value or []
        self._scalar_value = scalar_value

    def one(self):
        return self._one_value

    def all(self):
        return self._all_value

    def scalar_one(self):
        return self._scalar_value

    def mappings(self):
        return SimpleNamespace(all=lambda: self._mappings_value)


class FakeAsyncSession:
    def __init__(self, results: list[FakeResult]) -> None:
        self._results = results

    async def execute(self, _statement, _params=None):
        if not self._results:
            raise AssertionError("Unexpected execute call")
        return self._results.pop(0)


def test_cost_summary_endpoint_registered(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_cost_summary(*_args, **_kwargs):
        return CostSummaryResponse(
            total_tokens_mtd=1500000,
            total_estimated_cost_usd=0.225,
            avg_cost_per_call=0.0015,
            top_5_tenants_by_cost=[],
            total_gate_blocks_mtd=2,
            estimated_savings_from_gate_usd=0.003,
            projected_month_cost_usd=0.675,
            cost_is_estimate=True,
        )

    monkeypatch.setattr("api.routers.internal._get_system_cost_summary", fake_cost_summary)
    app = create_app()

    with TestClient(app) as client:
        response = client.get("/v1/internal/cost-summary", headers={"X-Admin-Secret": VALID_ADMIN_SECRET})

    assert response.status_code == 200
    assert response.json()["cost_is_estimate"] is True


@pytest.mark.asyncio
async def test_cost_summary_avg_cost_per_call_is_none_when_no_calls() -> None:
    tenant_id = uuid.uuid4()
    session = FakeAsyncSession(
        [
            FakeResult(one_value=SimpleNamespace(total_tokens=1_500_000, total_calls=0)),
            FakeResult(all_value=[SimpleNamespace(tenant_id=tenant_id, company_name="Acme", tokens=1_000_000)]),
            FakeResult(one_value=SimpleNamespace(blocked_calls=3)),
        ]
    )

    result = await _get_system_cost_summary(session)

    assert result.total_estimated_cost_usd == 0.225
    assert result.avg_cost_per_call is None
    assert result.estimated_savings_from_gate_usd == 0.0
    assert result.top_5_tenants_by_cost[0].tenant_id == tenant_id


def test_backfill_status_endpoint_registered(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_backfill_jobs(*_args, **_kwargs):
        return [
            BackfillJobResponse(
                id=uuid.uuid4(),
                task_name="reindex_proxy_users",
                status="running",
                total_rows=100,
                processed_rows=40,
                pct_complete=40.0,
                started_at=datetime.now(UTC),
                completed_at=None,
                error=None,
                eta_seconds=90,
            )
        ]

    monkeypatch.setattr("api.routers.internal._list_backfill_jobs", fake_backfill_jobs)
    app = create_app()

    with TestClient(app) as client:
        response = client.get("/v1/internal/backfill-status", headers={"X-Admin-Secret": VALID_ADMIN_SECRET})

    assert response.status_code == 200
    payload = response.json()
    assert isinstance(payload, list)
    assert payload[0]["task_name"] == "reindex_proxy_users"


@pytest.mark.asyncio
async def test_backfill_status_eta_is_none_when_processed_rows_zero() -> None:
    started_at = datetime.now(UTC)
    session = FakeAsyncSession(
        [
            FakeResult(
                mappings_value=[
                    {
                        "id": uuid.uuid4(),
                        "task_name": "reindex_proxy_users",
                        "status": "running",
                        "total_rows": 100,
                        "processed_rows": 0,
                        "started_at": started_at,
                        "completed_at": None,
                        "error": None,
                    }
                ]
            )
        ]
    )

    result = await _list_backfill_jobs(session)

    assert len(result) == 1
    assert result[0].pct_complete == 0.0
    assert result[0].eta_seconds is None


def test_audit_logs_endpoint_registered(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_audit_logs(*_args, **_kwargs):
        return AuditLogsResponse(
            data=[
                {
                    "id": str(uuid.uuid4()),
                    "tenant_id": str(uuid.uuid4()),
                    "company_name": "Acme",
                    "action": "memory_deleted",
                    "memory_id": None,
                    "created_at": datetime.now(UTC),
                    "ip_address": "127.0.0.1",
                    "old_value_summary": '{"content":"old"}',
                    "new_value_summary": None,
                    "metadata": {"source": "operator"},
                }
            ],
            next_cursor="50",
            total_count=1,
            start_date=datetime.now(UTC).date(),
            end_date=datetime.now(UTC).date(),
        )

    monkeypatch.setattr("api.routers.internal._list_audit_logs", fake_audit_logs)
    app = create_app()

    with TestClient(app) as client:
        response = client.get("/v1/internal/audit-logs", headers={"X-Admin-Secret": VALID_ADMIN_SECRET})

    assert response.status_code == 200
    payload = response.json()
    assert payload["total_count"] == 1
    assert payload["data"][0]["company_name"] == "Acme"
