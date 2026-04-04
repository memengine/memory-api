from __future__ import annotations

import json
import uuid
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from unittest.mock import AsyncMock

import pytest

from api.db.models import OveragePolicy
from api.db.models import PlanTier
from api.db.models import QuotaMode
from api.db.models import TenantBudget
from api.services.quota_manager import QuotaEnvelope
from api.services.quota_manager import QuotaManager
from api.services.webhook_event_service import WEBHOOK_EVENT_TASK_NAME


class FakeScalarResult:
    def __init__(self, item) -> None:
        self.item = item

    def scalar_one_or_none(self):
        return self.item


class FakeRedis:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}
        self.expirations: dict[str, int] = {}

    async def get(self, key: str):
        return self.values.get(key)

    async def set(self, key: str, value: str, ex: int | None = None):
        self.values[key] = value
        if ex is not None:
            self.expirations[key] = ex
        return True

    async def delete(self, key: str):
        self.values.pop(key, None)
        self.expirations.pop(key, None)
        return True


class FakeSession:
    def __init__(self, tenant_budget: TenantBudget | None) -> None:
        self.tenant_budget = tenant_budget
        self.execute = AsyncMock(side_effect=self._execute)
        self.commit = AsyncMock()

    async def _execute(self, _query):
        return FakeScalarResult(self.tenant_budget)


def make_budget(**overrides) -> TenantBudget:
    base = dict(
        id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        plan_tier=PlanTier.starter,
        monthly_call_limit=1000,
        monthly_token_limit=10000,
        current_month_calls=10,
        current_month_tokens=100,
        write_calls=5,
        write_call_limit=100,
        read_calls=5,
        read_limit=100,
        rate_limit_per_user_per_minute=10,
        overage_policy=OveragePolicy.warn,
        alert_threshold_pct=0.8,
        alert_webhook_url="https://tenant.example.test/webhook",
        webhook_secret="testsecret",
        last_notified_pct=None,
        last_notified_mode=QuotaMode.full,
        reset_at=datetime.now(UTC) + timedelta(days=12),
        created_at=datetime.now(UTC),
    )
    base.update(overrides)
    return TenantBudget(**base)


def build_manager(*, tenant_budget: TenantBudget | None):
    redis_client = FakeRedis()
    cache_service = type("CacheServiceStub", (), {"client": redis_client})()
    session = FakeSession(tenant_budget)
    dispatch_task = AsyncMock()
    manager = QuotaManager(
        session=session,
        cache_service=cache_service,
        dispatch_task=dispatch_task,
    )
    return manager, session, redis_client, dispatch_task


@pytest.mark.asyncio
async def test_quota_manager_returns_full_mode_and_caches_envelope() -> None:
    budget = make_budget(
        current_month_calls=12,
        current_month_tokens=120,
        write_calls=3,
        read_calls=9,
        last_notified_mode=QuotaMode.full,
    )
    manager, session, redis_client, dispatch_task = build_manager(tenant_budget=budget)

    mode = await manager.get_mode(str(budget.tenant_id))
    envelope = await manager.get_quota_envelope(str(budget.tenant_id))

    assert mode == QuotaMode.full
    assert isinstance(envelope, QuotaEnvelope)
    assert envelope.mode == QuotaMode.full
    assert envelope.budget_remaining_pct > 0.0
    assert session.execute.await_count == 1
    assert dispatch_task.await_count == 0

    cached_payload = json.loads(redis_client.values[manager._cache_key(str(budget.tenant_id))])
    assert cached_payload["mode"] == "FULL"
    assert redis_client.expirations[manager._cache_key(str(budget.tenant_id))] == 300


@pytest.mark.asyncio
async def test_quota_manager_transitions_to_passthrough_and_alerts_once() -> None:
    budget = make_budget(
        current_month_calls=1000,
        monthly_call_limit=1000,
        overage_policy=OveragePolicy.warn,
        last_notified_mode=QuotaMode.full,
    )
    manager, session, _redis_client, dispatch_task = build_manager(tenant_budget=budget)

    first_mode = await manager.get_mode(str(budget.tenant_id))
    await manager.invalidate_cache(str(budget.tenant_id))
    second_mode = await manager.get_mode(str(budget.tenant_id))

    assert first_mode == QuotaMode.passthrough
    assert second_mode == QuotaMode.passthrough
    assert session.commit.await_count == 1
    assert budget.last_notified_mode == QuotaMode.passthrough
    assert dispatch_task.await_count == 3
    events = [call.args[1][1] for call in dispatch_task.await_args_list]
    assert events == ["quota.critical", "mode.changed", "quota.exhausted"]
    assert all(call.args[0] == WEBHOOK_EVENT_TASK_NAME for call in dispatch_task.await_args_list)


@pytest.mark.asyncio
async def test_quota_manager_transitions_to_blocked() -> None:
    budget = make_budget(
        current_month_calls=1000,
        monthly_call_limit=1000,
        overage_policy=OveragePolicy.block,
        last_notified_mode=QuotaMode.full,
    )
    manager, session, _redis_client, dispatch_task = build_manager(tenant_budget=budget)

    envelope = await manager.get_quota_envelope(str(budget.tenant_id))

    assert envelope.mode == QuotaMode.blocked
    assert envelope.budget_remaining_pct == 0.0
    assert session.commit.await_count == 1
    assert dispatch_task.await_count == 3


@pytest.mark.asyncio
async def test_quota_manager_sends_warning_when_remaining_pct_crosses_threshold_without_mode_change() -> None:
    budget = make_budget(
        current_month_calls=250,
        monthly_call_limit=1000,
        overage_policy=OveragePolicy.warn,
        last_notified_mode=QuotaMode.full,
        last_notified_pct=None,
        alert_threshold_pct=0.8,
    )
    manager, session, _redis_client, dispatch_task = build_manager(tenant_budget=budget)

    envelope = await manager.get_quota_envelope(str(budget.tenant_id))

    assert envelope.mode == QuotaMode.full
    assert session.commit.await_count == 1
    assert budget.last_notified_pct == 0.8
    assert dispatch_task.await_count == 1
    task_name, args = dispatch_task.await_args.args
    assert task_name == WEBHOOK_EVENT_TASK_NAME
    assert args[1] == "quota.warning"


@pytest.mark.asyncio
async def test_quota_manager_transitions_to_degraded_retrieve_without_alert() -> None:
    budget = make_budget(
        write_calls=100,
        write_call_limit=100,
        read_calls=25,
        read_limit=100,
        current_month_calls=200,
        monthly_call_limit=1000,
        last_notified_mode=QuotaMode.full,
        last_notified_pct=0.05,
    )
    manager, session, _redis_client, dispatch_task = build_manager(tenant_budget=budget)

    mode = await manager.get_mode(str(budget.tenant_id))

    assert mode == QuotaMode.degraded_retrieve
    assert session.commit.await_count == 1
    assert budget.last_notified_mode == QuotaMode.degraded_retrieve
    assert dispatch_task.await_count == 1
    task_name, args = dispatch_task.await_args.args
    assert task_name == WEBHOOK_EVENT_TASK_NAME
    assert args[1] == "mode.changed"


@pytest.mark.asyncio
async def test_quota_manager_repeated_passthrough_checks_alert_once() -> None:
    budget = make_budget(
        current_month_calls=1000,
        monthly_call_limit=1000,
        overage_policy=OveragePolicy.warn,
        last_notified_mode=QuotaMode.full,
    )
    manager, session, _redis_client, dispatch_task = build_manager(tenant_budget=budget)

    for _ in range(101):
        await manager.invalidate_cache(str(budget.tenant_id))
        mode = await manager.get_mode(str(budget.tenant_id))
        assert mode == QuotaMode.passthrough

    assert session.commit.await_count == 1
    assert dispatch_task.await_count == 3
