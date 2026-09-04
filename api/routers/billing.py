from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request
from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from api.config.plan_limits import PLAN_LIMITS
from api.db.database import get_db_session
from api.db.models import BillingSubscription, TenantBudget
from api.dependencies import get_authenticated_tenant_id
from api.errors import APIError
from api.routers.common import get_request_id, utc_now
from api.schemas.billing_schemas import (
    BillingCheckoutData,
    BillingCheckoutRequest,
    BillingCheckoutResponse,
    BillingPlan,
    BillingPlanFeatures,
    BillingPlanLimits,
    BillingSubscriptionData,
    BillingSubscriptionResponse,
)
from api.services.razorpay_billing_service import create_subscription, public_key_id

router = APIRouter(prefix="/v1/billing", tags=["billing"])

PLANS_CACHE_KEY = "billing:plans:v5"
PLANS_CACHE_TTL_SECONDS = 3600


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


def _plan_features(
    *,
    audit_log_days: int,
    support: str,
    reliability_note: str,
    data_residency: str = "IN1 only",
) -> BillingPlanFeatures:
    return BillingPlanFeatures(
        quality_gate=True,
        domain_schemas=True,
        cross_agent=True,
        conflict_resolution=True,
        multi_service_writers=True,
        audit_log_days=audit_log_days,
        support=support,
        reliability_note=reliability_note,
        data_residency=data_residency,
    )


def _build_plans() -> list[BillingPlan]:
    return [
        BillingPlan(
            name="free",
            display_name="Free",
            badge="Try everything",
            monthly_price_inr=0,
            annual_price_inr=0,
            monthly_price_usd=0,
            annual_price_usd=0,
            is_popular=False,
            cta_text="Start for free",
            cta_type="signup",
            limits=_limits(
                "free", overage_policy_label="API pauses when limit reached"
            ),
            features=_plan_features(
                audit_log_days=7,
                support="Community and docs",
                reliability_note="Best-effort access for evaluation",
            ),
        ),
        BillingPlan(
            name="starter",
            display_name="Starter",
            badge="Most Popular",
            monthly_price_inr=1800,
            annual_price_inr=18000,
            monthly_price_usd=22,
            annual_price_usd=220,
            is_popular=True,
            cta_text="Upgrade to Starter",
            cta_type="checkout",
            limits=_limits(
                "starter", overage_policy_label="AI continues without memory context"
            ),
            features=_plan_features(
                audit_log_days=30,
                support="Email support",
                reliability_note="Operational monitoring",
            ),
        ),
        BillingPlan(
            name="growth",
            display_name="Growth",
            badge="Growing teams",
            monthly_price_inr=6000,
            annual_price_inr=60000,
            monthly_price_usd=72,
            annual_price_usd=720,
            is_popular=False,
            cta_text="Upgrade to Growth",
            cta_type="checkout",
            limits=_limits(
                "growth", overage_policy_label="AI continues without memory context"
            ),
            features=_plan_features(
                audit_log_days=90,
                support="Priority email support",
                reliability_note="Priority incident review",
            ),
        ),
        BillingPlan(
            name="scale",
            display_name="Scale",
            badge="Production scale",
            monthly_price_inr=18000,
            annual_price_inr=180000,
            monthly_price_usd=216,
            annual_price_usd=2160,
            is_popular=False,
            cta_text="Upgrade to Scale",
            cta_type="checkout",
            limits=_limits(
                "scale", overage_policy_label="AI continues without memory context"
            ),
            features=_plan_features(
                audit_log_days=180,
                support="Priority support and onboarding",
                reliability_note="Capacity planning and launch review",
            ),
        ),
        BillingPlan(
            name="enterprise",
            display_name="Enterprise",
            badge="Custom",
            monthly_price_inr=None,
            annual_price_inr=None,
            monthly_price_usd=None,
            annual_price_usd=None,
            is_popular=False,
            cta_text="Talk to Sales",
            cta_type="sales",
            limits=BillingPlanLimits(),
            features=_plan_features(
                audit_log_days=365,
                support="Custom success and procurement",
                reliability_note="Custom reliability terms by agreement",
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
    except Exception:  # noqa: BLE001 - the public catalog must survive cache failures
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
        return
    try:
        await client.set(PLANS_CACHE_KEY, json.dumps(plans), ex=PLANS_CACHE_TTL_SECONDS)
    except Exception:  # noqa: BLE001 - cache writes are best effort
        return


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


@router.post("/checkout", response_model=BillingCheckoutResponse)
async def create_billing_checkout(
    request: Request,
    payload: BillingCheckoutRequest,
    session: Annotated[AsyncSession, Depends(get_db_session)],
    tenant_id: Annotated[str, Depends(get_authenticated_tenant_id)],
) -> BillingCheckoutResponse:
    tenant_uuid = uuid.UUID(tenant_id)
    active_subscription = (
        await session.execute(
            select(BillingSubscription)
            .where(
                BillingSubscription.tenant_id == tenant_uuid,
                BillingSubscription.status.in_(("authenticated", "active")),
            )
            .limit(1)
        )
    ).scalar_one_or_none()
    if active_subscription is not None:
        raise APIError(
            status_code=409,
            code="BILL_409",
            error="active_subscription_already_exists",
        )

    reusable_after = datetime.now(UTC) - timedelta(minutes=30)
    existing = (
        await session.execute(
            select(BillingSubscription)
            .where(
                BillingSubscription.tenant_id == tenant_uuid,
                BillingSubscription.plan_tier == payload.plan_tier,
                BillingSubscription.billing_interval == payload.billing_interval,
                BillingSubscription.currency == payload.currency,
                BillingSubscription.status == "created",
                BillingSubscription.created_at >= reusable_after,
            )
            .order_by(desc(BillingSubscription.created_at))
            .limit(1)
        )
    ).scalar_one_or_none()

    if existing is None:
        provider = await create_subscription(
            tenant_id=tenant_id,
            plan_tier=payload.plan_tier,
            billing_interval=payload.billing_interval,
            currency=payload.currency,
        )
        existing = BillingSubscription(
            tenant_id=tenant_uuid,
            provider_subscription_id=str(provider["id"]),
            provider_customer_id=str(provider.get("customer_id") or "") or None,
            plan_tier=payload.plan_tier,
            billing_interval=payload.billing_interval,
            currency=payload.currency,
            status=str(provider.get("status") or "created"),
            checkout_url=str(provider["short_url"]),
        )
        session.add(existing)
        await session.commit()

    if not existing.checkout_url:
        raise APIError(
            status_code=409, code="BILL_409", error="checkout_url_unavailable"
        )
    return BillingCheckoutResponse(
        data=BillingCheckoutData(
            key_id=public_key_id(),
            subscription_id=existing.provider_subscription_id,
            checkout_url=existing.checkout_url,
            plan_tier=payload.plan_tier,
            billing_interval=payload.billing_interval,
            currency=payload.currency,
        ),
        request_id=get_request_id(request),
        timestamp=utc_now(),
    )


@router.get("/subscription", response_model=BillingSubscriptionResponse)
async def get_billing_subscription(
    request: Request,
    session: Annotated[AsyncSession, Depends(get_db_session)],
    tenant_id: Annotated[str, Depends(get_authenticated_tenant_id)],
) -> BillingSubscriptionResponse:
    tenant_uuid = uuid.UUID(tenant_id)
    subscription = (
        await session.execute(
            select(BillingSubscription)
            .where(BillingSubscription.tenant_id == tenant_uuid)
            .order_by(desc(BillingSubscription.created_at))
            .limit(1)
        )
    ).scalar_one_or_none()
    budget = (
        await session.execute(
            select(TenantBudget).where(TenantBudget.tenant_id == tenant_uuid)
        )
    ).scalar_one_or_none()
    plan_tier = str(
        getattr(getattr(budget, "plan_tier", None), "value", None) or "free"
    )
    return BillingSubscriptionResponse(
        data=BillingSubscriptionData(
            provider="razorpay" if subscription else None,
            provider_subscription_id=subscription.provider_subscription_id
            if subscription
            else None,
            plan_tier=plan_tier,
            billing_interval=subscription.billing_interval if subscription else None,
            currency=subscription.currency if subscription else None,
            status=subscription.status if subscription else "free",
            current_period_end=subscription.current_end if subscription else None,
            cancel_at_period_end=subscription.cancel_at_period_end
            if subscription
            else False,
            limits=_limits(plan_tier, overage_policy_label=None),
        ),
        request_id=get_request_id(request),
        timestamp=utc_now(),
    )
