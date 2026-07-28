from __future__ import annotations

import uuid
from datetime import UTC
from datetime import datetime

import pytest

from api.config import plan_limits as plan_limits_module
from api.config.plan_limits import apply_plan_limits
from api.config.plan_limits import get_limits
from api.db.models import TenantBudget
from api.db.models import PlanTier
from scripts.create_tenant import create_tenant_with_api_key


class FakeBudgetSession:
    def __init__(self) -> None:
        self.budgets: dict[str, TenantBudget] = {}
        self.tenants = []
        self.api_keys = []
        self.commit_calls = 0
        self.last_update_params = None

    def add(self, instance) -> None:
        if isinstance(instance, TenantBudget):
            self.budgets[str(instance.tenant_id)] = instance
        elif instance.__class__.__name__ == "Tenant":
            self.tenants.append(instance)
        elif instance.__class__.__name__ == "ApiKey":
            self.api_keys.append(instance)

    def flush(self) -> None:
        return None

    def commit(self) -> None:
        self.commit_calls += 1

    def refresh(self, _instance) -> None:
        return None

    def execute(self, statement, params=None):
        self.last_update_params = params
        tenant_id = str(params["tenant_id"])
        budget = self.budgets[tenant_id]
        budget.plan_tier = PlanTier(params["plan_tier"])
        budget.monthly_call_limit = params["monthly_call_limit"]
        budget.monthly_token_limit = params["monthly_token_limit"]
        budget.write_call_limit = params["write_call_limit"]
        budget.read_limit = params["read_limit"]
        budget.rate_limit_per_user_per_minute = params["rate_limit_per_user_per_minute"]
        budget.overage_policy = params["overage_policy"]
        budget.alert_threshold_pct = params["alert_threshold_pct"]
        budget.reset_at = datetime.now(UTC)


def make_budget(*, plan_tier: PlanTier = PlanTier.starter) -> TenantBudget:
    return TenantBudget(
        id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        plan_tier=plan_tier,
        monthly_call_limit=None,
        monthly_token_limit=None,
        current_month_calls=21,
        current_month_tokens=281,
        write_calls=7,
        write_call_limit=None,
        read_calls=14,
        read_limit=None,
        rate_limit_per_user_per_minute=None,
        overage_policy="warn",
        alert_threshold_pct=0.8,
        last_notified_mode=None,
        last_notified_pct=None,
        alert_webhook_url="https://example.com/webhook",
        webhook_secret="secret123",
        created_at=datetime.now(UTC),
        reset_at=None,
    )


def test_get_limits_starter() -> None:
    limits = get_limits("starter")

    assert limits == {
        "monthly_call_limit": 50_000,
        "monthly_token_limit": 25_000_000,
        "write_call_limit": 50_000,
        "read_limit": None,
        "rate_limit_per_user_per_minute": 10,
        "overage_policy": "warn",
        "alert_threshold_pct": 0.8,
    }


def test_get_limits_scale() -> None:
    limits = get_limits("scale")

    assert limits == {
        "monthly_call_limit": 1_000_000,
        "monthly_token_limit": 500_000_000,
        "write_call_limit": 1_000_000,
        "read_limit": None,
        "rate_limit_per_user_per_minute": 80,
        "overage_policy": "warn",
        "alert_threshold_pct": 0.9,
    }


def test_get_limits_enterprise() -> None:
    limits = get_limits("enterprise")

    assert limits["monthly_call_limit"] is None
    assert limits["monthly_token_limit"] is None
    assert limits["write_call_limit"] is None
    assert limits["read_limit"] is None
    assert limits["rate_limit_per_user_per_minute"] is None
    assert limits["overage_policy"] == "warn"
    assert limits["alert_threshold_pct"] == 0.9


def test_get_limits_invalid() -> None:
    with pytest.raises(ValueError, match="Unknown plan tier"):
        get_limits("pro")


def test_apply_plan_limits_sets_all_columns(monkeypatch) -> None:
    deleted_keys = []
    monkeypatch.setattr(
        plan_limits_module,
        "_invalidate_plan_cache",
        lambda tenant_id: deleted_keys.append(tenant_id),
    )
    session = FakeBudgetSession()
    budget = make_budget()
    original_counters = (
        budget.current_month_calls,
        budget.current_month_tokens,
        budget.write_calls,
        budget.read_calls,
    )
    session.add(budget)

    apply_plan_limits(str(budget.tenant_id), "starter", session)

    assert budget.monthly_call_limit == 50_000
    assert budget.monthly_token_limit == 25_000_000
    assert budget.write_call_limit == 50_000
    assert budget.read_limit is None
    assert budget.rate_limit_per_user_per_minute == 10
    assert budget.overage_policy == "warn"
    assert budget.alert_threshold_pct == 0.8
    assert budget.reset_at is not None
    assert (
        budget.current_month_calls,
        budget.current_month_tokens,
        budget.write_calls,
        budget.read_calls,
    ) == original_counters
    assert session.commit_calls == 1
    assert deleted_keys == [str(budget.tenant_id)]


def test_apply_plan_limits_idempotent(monkeypatch) -> None:
    monkeypatch.setattr(plan_limits_module, "_invalidate_plan_cache", lambda tenant_id: None)
    session = FakeBudgetSession()
    budget = make_budget()
    session.add(budget)

    apply_plan_limits(str(budget.tenant_id), "starter", session)
    first_state = (
        budget.plan_tier,
        budget.monthly_call_limit,
        budget.monthly_token_limit,
        budget.write_call_limit,
        budget.read_limit,
        budget.rate_limit_per_user_per_minute,
        budget.overage_policy,
        budget.alert_threshold_pct,
    )
    apply_plan_limits(str(budget.tenant_id), "starter", session)
    second_state = (
        budget.plan_tier,
        budget.monthly_call_limit,
        budget.monthly_token_limit,
        budget.write_call_limit,
        budget.read_limit,
        budget.rate_limit_per_user_per_minute,
        budget.overage_policy,
        budget.alert_threshold_pct,
    )

    assert first_state == second_state


def test_new_tenant_gets_starter_limits(monkeypatch) -> None:
    monkeypatch.setattr(plan_limits_module, "_invalidate_plan_cache", lambda tenant_id: None)
    session = FakeBudgetSession()

    tenant, raw_api_key = create_tenant_with_api_key(
        session=session,
        company_name="Starter Tenant",
        api_key_name="Primary SDK Key",
    )

    budget = session.budgets[str(tenant.id)]
    assert budget.plan_tier == PlanTier.starter
    assert budget.monthly_call_limit == 50_000
    assert budget.monthly_token_limit == 25_000_000
    assert budget.write_call_limit == 50_000
    assert budget.read_limit is None
    assert budget.rate_limit_per_user_per_minute == 10
    assert budget.reset_at is not None
    assert raw_api_key.startswith("mem_")
