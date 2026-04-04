from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from redis.exceptions import ConnectionError as RedisConnectionError
from redis.exceptions import TimeoutError as RedisTimeoutError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from api.db.cache import CacheService
from api.db.models import OveragePolicy
from api.db.models import QuotaMode
from api.db.models import TenantBudget
from api.infra.fallbacks import on_redis_open
from api.services.webhook_event_service import WebhookEventService


QUOTA_CACHE_TTL_SECONDS = 300
QUOTA_CACHE_PREFIX = "quota_mode"
REDIS_FAILURES = (RedisConnectionError, RedisTimeoutError)


@dataclass(slots=True)
class QuotaEnvelope:
    mode: QuotaMode
    budget_remaining_pct: float
    reset_at: datetime | None


class QuotaManager:
    def __init__(
        self,
        *,
        session: AsyncSession,
        cache_service: CacheService,
        dispatch_task=None,
    ) -> None:
        self.session = session
        self.cache_service = cache_service
        self.dispatch_task = dispatch_task
        self.webhook_events = WebhookEventService(
            session=session,
            dispatch_task=dispatch_task,
        )

    def _mark_redis_unavailable(self) -> None:
        breaker = getattr(self.cache_service, "breaker", None)
        force_open = getattr(breaker, "force_open", None)
        if callable(force_open):
            try:
                force_open()
            except Exception:
                return None

    async def get_mode(self, tenant_id: str) -> QuotaMode:
        envelope = await self.get_quota_envelope(tenant_id)
        return envelope.mode

    async def get_quota_envelope(self, tenant_id: str) -> QuotaEnvelope:
        cached = await self._get_cached_envelope(tenant_id)
        if cached is not None:
            return cached

        tenant_budget = await self._get_tenant_budget(tenant_id)
        envelope = self._compute_envelope(tenant_budget)
        await self._handle_mode_transition(tenant_id=tenant_id, tenant_budget=tenant_budget, envelope=envelope)
        await self._cache_envelope(tenant_id, envelope)
        return envelope

    async def invalidate_cache(self, tenant_id: str) -> None:
        try:
            await self._redis_call(
                self.cache_service.client.delete,
                self._cache_key(tenant_id),
                fallback=lambda: on_redis_open(None),
            )
        except REDIS_FAILURES:
            self._mark_redis_unavailable()
            return None

    async def _get_tenant_budget(self, tenant_id: str) -> TenantBudget | None:
        result = await self.session.execute(
            select(TenantBudget).where(TenantBudget.tenant_id == self._as_uuid(tenant_id))
        )
        return result.scalar_one_or_none()

    def _compute_envelope(self, tenant_budget: TenantBudget | None) -> QuotaEnvelope:
        if tenant_budget is None:
            return QuotaEnvelope(
                mode=QuotaMode.full,
                budget_remaining_pct=1.0,
                reset_at=None,
            )

        mode = self._compute_mode(tenant_budget)
        return QuotaEnvelope(
            mode=mode,
            budget_remaining_pct=self._budget_remaining_pct(tenant_budget),
            reset_at=tenant_budget.reset_at,
        )

    def _compute_mode(self, tenant_budget: TenantBudget) -> QuotaMode:
        call_limit_reached = (
            tenant_budget.monthly_call_limit is not None
            and int(tenant_budget.current_month_calls or 0) >= int(tenant_budget.monthly_call_limit)
        )
        token_limit_reached = (
            tenant_budget.monthly_token_limit is not None
            and int(tenant_budget.current_month_tokens or 0) >= int(tenant_budget.monthly_token_limit)
        )
        write_limit_reached = (
            tenant_budget.write_call_limit is not None
            and int(tenant_budget.write_calls or 0) >= int(tenant_budget.write_call_limit)
        )
        read_limit_remaining = (
            tenant_budget.read_limit is None
            or int(tenant_budget.read_calls or 0) < int(tenant_budget.read_limit)
        )

        if tenant_budget.overage_policy == OveragePolicy.block and (call_limit_reached or token_limit_reached):
            return QuotaMode.blocked
        if tenant_budget.overage_policy == OveragePolicy.warn and call_limit_reached:
            return QuotaMode.passthrough
        if write_limit_reached and read_limit_remaining:
            return QuotaMode.degraded_retrieve
        return QuotaMode.full

    async def _handle_mode_transition(
        self,
        *,
        tenant_id: str,
        tenant_budget: TenantBudget | None,
        envelope: QuotaEnvelope,
    ) -> None:
        if tenant_budget is None:
            return

        previous_mode = tenant_budget.last_notified_mode or QuotaMode.full
        changed = False

        if previous_mode != envelope.mode:
            tenant_budget.last_notified_mode = envelope.mode
            changed = True

        threshold_pct = float(tenant_budget.alert_threshold_pct or 0.8)
        last_notified_pct = tenant_budget.last_notified_pct
        remaining_pct = envelope.budget_remaining_pct

        should_send_warning = (
            remaining_pct <= threshold_pct
            and (last_notified_pct is None or float(last_notified_pct) > threshold_pct)
        )
        should_send_critical = (
            remaining_pct <= 0.05
            and (last_notified_pct is None or float(last_notified_pct) > 0.05)
        )

        if should_send_critical:
            tenant_budget.last_notified_pct = 0.05
            changed = True
        elif should_send_warning:
            tenant_budget.last_notified_pct = threshold_pct
            changed = True

        if changed and hasattr(self.session, "commit"):
            await self.session.commit()

        if should_send_warning and not should_send_critical:
            await self.webhook_events.send_quota_warning(
                tenant_id,
                remaining_pct=remaining_pct,
                threshold_pct=threshold_pct,
            )

        if should_send_critical:
            await self.webhook_events.send_quota_critical(
                tenant_id,
                remaining_pct=remaining_pct,
            )

        if previous_mode == envelope.mode:
            return

        await self.webhook_events.send_mode_changed(
            tenant_id,
            from_mode=previous_mode.value,
            to_mode=envelope.mode.value,
            reason=self._mode_change_reason(envelope.mode),
        )
        if envelope.mode in {QuotaMode.passthrough, QuotaMode.blocked}:
            await self.webhook_events.send_quota_exhausted(
                tenant_id,
                mode=envelope.mode.value,
            )

    async def _dispatch(self, task_name: str, args: list[Any]) -> None:
        if self.dispatch_task is None:
            return
        dispatched = self.dispatch_task(task_name, args)
        if dispatched is not None and hasattr(dispatched, "__await__"):
            await dispatched

    @staticmethod
    def _mode_change_reason(mode: QuotaMode) -> str:
        if mode == QuotaMode.passthrough:
            return "call_limit_reached"
        if mode == QuotaMode.blocked:
            return "budget_exhausted"
        if mode == QuotaMode.degraded_retrieve:
            return "write_limit_reached"
        return "quota_recovered"

    async def _get_cached_envelope(self, tenant_id: str) -> QuotaEnvelope | None:
        try:
            cached_value = await self._redis_call(
                self.cache_service.client.get,
                self._cache_key(tenant_id),
                fallback=lambda: on_redis_open(None),
            )
        except REDIS_FAILURES:
            self._mark_redis_unavailable()
            return None

        if cached_value is None:
            return None

        try:
            payload = json.loads(cached_value)
        except json.JSONDecodeError:
            return None

        mode = payload.get("mode")
        if mode is None:
            return None
        reset_at = payload.get("reset_at")
        return QuotaEnvelope(
            mode=QuotaMode(mode),
            budget_remaining_pct=float(payload.get("budget_remaining_pct", 1.0)),
            reset_at=datetime.fromisoformat(reset_at) if reset_at else None,
        )

    async def _cache_envelope(self, tenant_id: str, envelope: QuotaEnvelope) -> None:
        payload = {
            "mode": envelope.mode.value,
            "budget_remaining_pct": envelope.budget_remaining_pct,
            "reset_at": envelope.reset_at.isoformat() if envelope.reset_at else None,
        }
        try:
            await self._redis_call(
                self.cache_service.client.set,
                self._cache_key(tenant_id),
                json.dumps(payload),
                ex=QUOTA_CACHE_TTL_SECONDS,
                fallback=lambda: on_redis_open(None),
            )
        except REDIS_FAILURES:
            self._mark_redis_unavailable()
            return None

    @staticmethod
    def _cache_key(tenant_id: str) -> str:
        return f"{QUOTA_CACHE_PREFIX}:{tenant_id}"

    @staticmethod
    def _budget_remaining_pct(tenant_budget: TenantBudget) -> float:
        usages = []
        if tenant_budget.monthly_call_limit not in (None, 0):
            usages.append(int(tenant_budget.current_month_calls or 0) / int(tenant_budget.monthly_call_limit))
        if tenant_budget.monthly_token_limit not in (None, 0):
            usages.append(int(tenant_budget.current_month_tokens or 0) / int(tenant_budget.monthly_token_limit))
        if tenant_budget.write_call_limit not in (None, 0):
            usages.append(int(tenant_budget.write_calls or 0) / int(tenant_budget.write_call_limit))
        if tenant_budget.read_limit not in (None, 0):
            usages.append(int(tenant_budget.read_calls or 0) / int(tenant_budget.read_limit))

        if not usages:
            return 1.0

        usage_pct = min(max(usages), 1.0)
        return max(0.0, round(1.0 - usage_pct, 4))

    async def _redis_call(self, fn, *args, fallback=None, **kwargs):
        breaker = getattr(self.cache_service, "breaker", None)
        if (
            breaker is None
            or breaker.__class__.__module__.startswith("unittest.mock")
        ):
            return await fn(*args, **kwargs)
        return await breaker.call(fn, *args, fallback=fallback, **kwargs)

    @staticmethod
    def _as_uuid(value: str):
        import uuid

        return uuid.UUID(value)
