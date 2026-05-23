from __future__ import annotations

import json
import os
from typing import Any

from fastapi import APIRouter
from fastapi import Request

from api.config.plan_limits import PLAN_LIMITS
from api.schemas.billing_schemas import BillingPlan
from api.schemas.billing_schemas import BillingPlanFeatures
from api.schemas.billing_schemas import BillingPlanLimits


router = APIRouter(prefix="/v1/billing", tags=["billing"])

PLANS_CACHE_KEY = "billing:plans:v1"
PLANS_CACHE_TTL_SECONDS = 3600


def _stripe_price(env_name: str) -> str | None:
    value = os.getenv(env_name)
    return value.strip() or None if value is not None else None


def _limits(plan_name: str, *, overage_policy_label: str | None) -> BillingPlanLimits:
    plan_limits = PLAN_LIMITS[plan_name]
    return BillingPlanLimits(
        monthly_call_limit=plan_limits["monthly_call_limit"],
        write_call_limit=plan_limits["write_call_limit"],
        read_limit=plan_limits["read_limit"],
        rate_limit_per_user_per_minute=plan_limits["rate_limit_per_user_per_minute"],
        overage_policy=plan_limits["overage_policy"],
        overage_policy_label=overage_policy_label,
    )


def _build_plans() -> list[BillingPlan]:
    return [
        BillingPlan(
            name="free",
            display_name="Free",
            badge="Always Free",
            monthly_price_inr=0,
            annual_price_inr=0,
            monthly_price_usd=0,
            annual_price_usd=0,
            is_popular=False,
            cta_text="Start for free",
            cta_type="signup",
            limits=_limits("free", overage_policy_label="API pauses when limit reached"),
            features=BillingPlanFeatures(
                quality_gate=True,
                domain_schemas=False,
                cross_agent=False,
                audit_log_days=0,
                support="Community",
                sla="Best effort",
                data_residency="IN1 only",
            ),
        ),
        BillingPlan(
            name="starter",
            display_name="Starter",
            badge="Most Popular",
            monthly_price_inr=999,
            annual_price_inr=9990,
            monthly_price_usd=12,
            annual_price_usd=120,
            is_popular=True,
            cta_text="Upgrade to Starter",
            cta_type="checkout",
            stripe_price_monthly=_stripe_price("STRIPE_PRICE_STARTER_MONTHLY"),
            stripe_price_annual=_stripe_price("STRIPE_PRICE_STARTER_ANNUAL"),
            limits=_limits("starter", overage_policy_label="AI continues without memory context"),
            features=BillingPlanFeatures(
                quality_gate=True,
                domain_schemas=False,
                cross_agent=False,
                audit_log_days=30,
                support="Email (48h SLA)",
                sla="99.5%",
                data_residency="IN1 only",
            ),
        ),
        BillingPlan(
            name="growth",
            display_name="Growth",
            badge="Scale Up",
            monthly_price_inr=3999,
            annual_price_inr=39990,
            monthly_price_usd=48,
            annual_price_usd=480,
            is_popular=False,
            cta_text="Upgrade to Growth",
            cta_type="checkout",
            stripe_price_monthly=_stripe_price("STRIPE_PRICE_GROWTH_MONTHLY"),
            stripe_price_annual=_stripe_price("STRIPE_PRICE_GROWTH_ANNUAL"),
            limits=_limits("growth", overage_policy_label="AI continues without memory context"),
            features=BillingPlanFeatures(
                quality_gate=True,
                domain_schemas=True,
                cross_agent=True,
                audit_log_days=90,
                support="Email (24h SLA)",
                sla="99.9%",
                data_residency="IN1 only",
            ),
        ),
        BillingPlan(
            name="enterprise",
            display_name="Enterprise",
            badge="Unlimited",
            monthly_price_inr=None,
            annual_price_inr=None,
            monthly_price_usd=None,
            annual_price_usd=None,
            is_popular=False,
            cta_text="Talk to Sales",
            cta_type="sales",
            limits=BillingPlanLimits(),
            features=BillingPlanFeatures(
                quality_gate=True,
                domain_schemas=True,
                cross_agent=True,
                audit_log_days=365,
                support="Dedicated Slack",
                sla="99.99%",
                data_residency="Choose region",
            ),
        ),
    ]


async def _read_cached_plans(request: Request) -> list[dict[str, Any]] | None:
    cache_service = getattr(request.app.state, "cache_service", None)
    client = getattr(cache_service, "client", None)
    if client is None:
        return None
    try:
        raw_value = await client.get(PLANS_CACHE_KEY)
    except Exception:
        return None
    if raw_value is None:
        return None
    try:
        payload = json.loads(raw_value)
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, list) else None


async def _cache_plans(request: Request, plans: list[dict[str, Any]]) -> None:
    cache_service = getattr(request.app.state, "cache_service", None)
    client = getattr(cache_service, "client", None)
    if client is None:
        return None
    try:
        await client.set(PLANS_CACHE_KEY, json.dumps(plans), ex=PLANS_CACHE_TTL_SECONDS)
    except Exception:
        return None


@router.get("/plans", response_model=list[BillingPlan])
async def list_billing_plans(request: Request) -> list[BillingPlan | dict[str, Any]]:
    """Return public pricing plan metadata for the pricing page."""
    cached_plans = await _read_cached_plans(request)
    if cached_plans is not None:
        return cached_plans

    plans = _build_plans()
    payload = [plan.model_dump(mode="json") for plan in plans]
    await _cache_plans(request, payload)
    return plans
