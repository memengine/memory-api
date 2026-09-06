from __future__ import annotations

import hashlib
import json
from asyncio import to_thread
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Request
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from api.config.plan_limits import get_limits, invalidate_plan_cache
from api.db.database import get_db_session
from api.db.models import (
    BillingSubscription,
    BillingWebhookEvent,
    PlanTier,
    Tenant,
    TenantBudget,
)
from api.errors import APIError
from api.services.razorpay_billing_service import (
    configured_plan_id,
    unix_timestamp,
    verify_webhook_signature,
    webhook_event_id,
)

router = APIRouter(prefix="/v1/webhooks/razorpay", tags=["billing"])
MAX_WEBHOOK_BYTES = 1_048_576
ACTIVATION_EVENTS = {"subscription.activated", "subscription.charged"}
TERMINAL_EVENTS = {
    "subscription.cancelled",
    "subscription.completed",
    "subscription.expired",
    "subscription.halted",
}
CURRENT_SUBSCRIPTION_STATUSES = {"authenticated", "active"}


@router.post("")
async def receive_razorpay_webhook(
    request: Request,
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> dict[str, bool]:
    raw_body = await request.body()
    if len(raw_body) > MAX_WEBHOOK_BYTES:
        raise APIError(
            status_code=413, code="BILL_413", error="webhook_payload_too_large"
        )
    verify_webhook_signature(raw_body, request.headers.get("x-razorpay-signature"))
    try:
        payload = json.loads(raw_body)
    except json.JSONDecodeError as exc:
        raise APIError(
            status_code=400, code="BILL_400", error="invalid_webhook_payload"
        ) from exc

    event_type = str(payload.get("event") or "").strip()
    event_id = webhook_event_id(raw_body, request.headers.get("x-razorpay-event-id"))
    if await session.get(BillingWebhookEvent, event_id) is not None:
        return {"received": True}

    receipt = BillingWebhookEvent(
        provider_event_id=event_id,
        event_type=event_type or "unknown",
        payload_sha256=hashlib.sha256(raw_body).hexdigest(),
    )
    session.add(receipt)
    try:
        await session.flush()
    except IntegrityError:
        await session.rollback()
        return {"received": True}

    entity = ((payload.get("payload") or {}).get("subscription") or {}).get(
        "entity"
    ) or {}
    provider_subscription_id = str(entity.get("id") or "").strip()
    subscription = None
    if provider_subscription_id:
        subscription = (
            await session.execute(
                select(BillingSubscription).where(
                    BillingSubscription.provider_subscription_id
                    == provider_subscription_id
                )
            )
        ).scalar_one_or_none()

    if subscription is None:
        await session.rollback()
        raise APIError(
            status_code=503,
            code="BILL_503",
            error="subscription_not_ready",
        )

    expected_plan_id = configured_plan_id(
        subscription.plan_tier.value,
        subscription.billing_interval,
        subscription.currency,
    )
    if entity.get("plan_id") != expected_plan_id:
        await session.rollback()
        raise APIError(status_code=409, code="BILL_409", error="provider_plan_mismatch")

    subscription.status = str(entity.get("status") or subscription.status)
    subscription.provider_customer_id = (
        str(entity.get("customer_id") or "") or subscription.provider_customer_id
    )
    subscription.current_start = unix_timestamp(entity.get("current_start"))
    subscription.current_end = unix_timestamp(entity.get("current_end"))
    subscription.cancel_at_period_end = bool(entity.get("has_scheduled_changes"))

    # Razorpay does not guarantee webhook delivery order. A delayed event for
    # an older subscription must not overwrite limits from a newer active one.
    newer_active_subscription = None
    if event_type in ACTIVATION_EVENTS | TERMINAL_EVENTS:
        newer_active_subscription = (
            await session.execute(
                select(BillingSubscription.id)
                .where(
                    BillingSubscription.tenant_id == subscription.tenant_id,
                    BillingSubscription.id != subscription.id,
                    BillingSubscription.status.in_(CURRENT_SUBSCRIPTION_STATUSES),
                    BillingSubscription.created_at > subscription.created_at,
                )
                .limit(1)
            )
        ).scalar_one_or_none()

    if event_type in ACTIVATION_EVENTS:
        if newer_active_subscription is None:
            await session.execute(
                update(TenantBudget)
                .where(TenantBudget.tenant_id == subscription.tenant_id)
                .values(
                    plan_tier=subscription.plan_tier,
                    **get_limits(subscription.plan_tier.value),
                )
            )
            await session.execute(
                update(Tenant)
                .where(Tenant.id == subscription.tenant_id)
                .values(plan_tier=subscription.plan_tier)
            )
    elif event_type in TERMINAL_EVENTS and newer_active_subscription is None:
        await session.execute(
            update(TenantBudget)
            .where(TenantBudget.tenant_id == subscription.tenant_id)
            .values(plan_tier=PlanTier.free, **get_limits(PlanTier.free.value))
        )
        await session.execute(
            update(Tenant)
            .where(Tenant.id == subscription.tenant_id)
            .values(plan_tier=PlanTier.free)
        )

    receipt.processed_at = datetime.now(UTC)
    await session.commit()
    if event_type in ACTIVATION_EVENTS or event_type in TERMINAL_EVENTS:
        await to_thread(invalidate_plan_cache, str(subscription.tenant_id))
    return {"received": True}
