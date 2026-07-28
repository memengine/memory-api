from __future__ import annotations

from typing import Any

import redis
from sqlalchemy import text

from api.db.cache import get_redis_url


PLAN_LIMITS = {
    "free": {
        "monthly_call_limit": 5_000,
        "monthly_token_limit": 2_000_000,
        "write_call_limit": 5_000,
        "read_limit": None,
        "rate_limit_per_user_per_minute": 3,
        "overage_policy": "block",
        "alert_threshold_pct": 0.8,
    },
    "starter": {
        "monthly_call_limit": 50_000,
        "monthly_token_limit": 25_000_000,
        "write_call_limit": 50_000,
        "read_limit": None,
        "rate_limit_per_user_per_minute": 10,
        "overage_policy": "warn",
        "alert_threshold_pct": 0.8,
    },
    "growth": {
        "monthly_call_limit": 500_000,
        "monthly_token_limit": 250_000_000,
        "write_call_limit": 500_000,
        "read_limit": None,
        "rate_limit_per_user_per_minute": 30,
        "overage_policy": "warn",
        "alert_threshold_pct": 0.8,
    },
    "scale": {
        "monthly_call_limit": 1_000_000,
        "monthly_token_limit": 500_000_000,
        "write_call_limit": 1_000_000,
        "read_limit": None,
        "rate_limit_per_user_per_minute": 80,
        "overage_policy": "warn",
        "alert_threshold_pct": 0.9,
    },
    "enterprise": {
        "monthly_call_limit": None,
        "monthly_token_limit": None,
        "write_call_limit": None,
        "read_limit": None,
        "rate_limit_per_user_per_minute": None,
        "overage_policy": "warn",
        "alert_threshold_pct": 0.9,
    },
}


def get_limits(plan_tier: str) -> dict[str, Any]:
    if plan_tier not in PLAN_LIMITS:
        raise ValueError(f"Unknown plan tier: {plan_tier}")
    return dict(PLAN_LIMITS[plan_tier])


def _invalidate_plan_cache(tenant_id: str) -> None:
    try:
        client = redis.Redis.from_url(
            get_redis_url(),
            encoding="utf-8",
            decode_responses=True,
        )
        try:
            client.delete(
                f"quota_mode:{tenant_id}",
                f"tenant:{tenant_id}:plan",
                f"tenant:{tenant_id}:region",
            )
        finally:
            client.close()
    except Exception:
        return None


def apply_plan_limits(tenant_id: str, plan_tier: str, db_session) -> None:
    limits = get_limits(plan_tier)
    db_session.execute(
        text(
            """
            UPDATE tenant_budgets
            SET
                plan_tier = :plan_tier,
                monthly_call_limit = :monthly_call_limit,
                monthly_token_limit = :monthly_token_limit,
                write_call_limit = :write_call_limit,
                read_limit = :read_limit,
                rate_limit_per_user_per_minute = :rate_limit_per_user_per_minute,
                overage_policy = :overage_policy,
                alert_threshold_pct = :alert_threshold_pct,
                reset_at = DATE_TRUNC('month', NOW() AT TIME ZONE 'UTC') + INTERVAL '1 month'
            WHERE tenant_id = :tenant_id
            """
        ),
        {**limits, "plan_tier": plan_tier, "tenant_id": tenant_id},
    )
    db_session.commit()
    _invalidate_plan_cache(tenant_id)
