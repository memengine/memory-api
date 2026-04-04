from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from typing import Any
from typing import Awaitable
from typing import Callable

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from api.db.models import OveragePolicy
from api.db.models import TenantBudget


LOGGER = logging.getLogger("memoryos.budget_governor")
DEFAULT_RATE_LIMIT_PER_USER_PER_MINUTE = 10
INCREMENT_BUDGET_TASK_NAME = "api.tasks.quality_gate_tasks.increment_tenant_budget_usage"
ALERT_BUDGET_TASK_NAME = "api.tasks.quality_gate_tasks.send_budget_alert"

DispatchTask = Callable[[str, list[Any]], Awaitable[None] | None]


@dataclass(slots=True)
class BudgetDecision:
    passed: bool
    reason: str | None
    estimated_tokens: int
    should_alert: bool
    overage_policy: str
    budget_remaining_pct: float
    should_flag_for_billing: bool = False
    should_warn: bool = False


class BudgetGovernor:
    def __init__(
        self,
        *,
        session: AsyncSession,
        dispatch_task: DispatchTask | None = None,
    ) -> None:
        self.session = session
        self.dispatch_task = dispatch_task

    async def get_tenant_budget(self, tenant_id: str) -> TenantBudget | None:
        result = await self.session.execute(
            select(TenantBudget).where(TenantBudget.tenant_id == self._as_uuid(tenant_id))
        )
        return result.scalar_one_or_none()

    async def evaluate(
        self,
        *,
        messages: list[dict[str, Any]],
        tenant_id: str,
        tenant_budget: TenantBudget | None = None,
    ) -> BudgetDecision:
        budget = tenant_budget or await self.get_tenant_budget(tenant_id)
        estimated_tokens = self.estimate_tokens(messages)

        if budget is None:
            return BudgetDecision(
                passed=True,
                reason=None,
                estimated_tokens=estimated_tokens,
                should_alert=False,
                overage_policy=OveragePolicy.warn.value,
                budget_remaining_pct=1.0,
            )

        current_calls = int(budget.current_month_calls or 0)
        current_tokens = int(budget.current_month_tokens or 0)
        monthly_call_limit = budget.monthly_call_limit
        monthly_token_limit = budget.monthly_token_limit
        overage_policy = budget.overage_policy.value

        call_usage_pct = (
            min(current_calls / monthly_call_limit, 1.0)
            if monthly_call_limit not in (None, 0)
            else 0.0
        )
        token_usage_pct = (
            min((current_tokens + estimated_tokens) / monthly_token_limit, 1.0)
            if monthly_token_limit not in (None, 0)
            else 0.0
        )
        usage_pct = max(call_usage_pct, token_usage_pct)
        remaining_pct = max(0.0, round(1.0 - usage_pct, 4))
        should_alert = usage_pct >= float(budget.alert_threshold_pct or 0.8)

        if should_alert:
            await self._dispatch(
                ALERT_BUDGET_TASK_NAME,
                [tenant_id, usage_pct, estimated_tokens],
            )

        call_limit_exhausted = (
            monthly_call_limit is not None and current_calls >= int(monthly_call_limit)
        )
        token_limit_exhausted = (
            monthly_token_limit is not None
            and (current_tokens + estimated_tokens) > int(monthly_token_limit)
        )

        if (call_limit_exhausted or token_limit_exhausted) and overage_policy == OveragePolicy.block.value:
            return BudgetDecision(
                passed=False,
                reason="budget_exhausted",
                estimated_tokens=estimated_tokens,
                should_alert=should_alert,
                overage_policy=overage_policy,
                budget_remaining_pct=remaining_pct,
            )

        if overage_policy == OveragePolicy.warn.value and (call_limit_exhausted or token_limit_exhausted):
            LOGGER.warning(
                "Tenant %s exceeded budget in warn mode",
                tenant_id,
            )

        return BudgetDecision(
            passed=True,
            reason=None,
            estimated_tokens=estimated_tokens,
            should_alert=should_alert,
            overage_policy=overage_policy,
            budget_remaining_pct=remaining_pct,
            should_flag_for_billing=overage_policy == OveragePolicy.charge.value,
            should_warn=overage_policy == OveragePolicy.warn.value and (call_limit_exhausted or token_limit_exhausted),
        )

    async def dispatch_usage_increment(self, tenant_id: str, estimated_tokens: int) -> None:
        await self._dispatch(INCREMENT_BUDGET_TASK_NAME, [tenant_id, estimated_tokens])

    def budget_remaining_pct(
        self,
        *,
        messages: list[dict[str, Any]],
        tenant_budget: TenantBudget | None,
    ) -> float:
        if tenant_budget is None:
            return 1.0

        estimated_tokens = self.estimate_tokens(messages)
        current_calls = int(tenant_budget.current_month_calls or 0)
        current_tokens = int(tenant_budget.current_month_tokens or 0)
        monthly_call_limit = tenant_budget.monthly_call_limit
        monthly_token_limit = tenant_budget.monthly_token_limit

        call_usage_pct = (
            min(current_calls / monthly_call_limit, 1.0)
            if monthly_call_limit not in (None, 0)
            else 0.0
        )
        token_usage_pct = (
            min((current_tokens + estimated_tokens) / monthly_token_limit, 1.0)
            if monthly_token_limit not in (None, 0)
            else 0.0
        )
        usage_pct = max(call_usage_pct, token_usage_pct)
        return max(0.0, round(1.0 - usage_pct, 4))

    @staticmethod
    def estimate_tokens(messages: list[dict[str, Any]]) -> int:
        total = 0
        for message in messages:
            content = str(message.get("content", "") or "")
            total += max(1, len(content) // 4)
        return total

    @staticmethod
    def rate_limit_per_user(tenant_budget: TenantBudget | None) -> int:
        if tenant_budget is None:
            return DEFAULT_RATE_LIMIT_PER_USER_PER_MINUTE
        return int(tenant_budget.rate_limit_per_user_per_minute or DEFAULT_RATE_LIMIT_PER_USER_PER_MINUTE)

    async def _dispatch(self, task_name: str, args: list[Any]) -> None:
        if self.dispatch_task is None:
            return
        dispatched = self.dispatch_task(task_name, args)
        if dispatched is not None and hasattr(dispatched, "__await__"):
            await dispatched

    @staticmethod
    def _as_uuid(value: str) -> uuid.UUID:
        return uuid.UUID(value)
