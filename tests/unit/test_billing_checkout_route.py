from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest
from starlette.requests import Request

from api.db.models import BillingSubscription, PlanTier
from api.errors import APIError
from api.routers import billing
from api.schemas.billing_schemas import (
    BillingCheckoutRequest,
    BillingCheckoutVerificationRequest,
)


def _request() -> Request:
    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/v1/billing/checkout",
            "headers": [],
        }
    )


def _query_result(value: object | None) -> MagicMock:
    result = MagicMock()
    result.scalar_one_or_none.return_value = value
    return result


@pytest.mark.asyncio
async def test_checkout_creates_provider_subscription_and_persists_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = MagicMock()
    session.execute = AsyncMock(
        side_effect=[_query_result(object()), _query_result(None), _query_result(None)]
    )
    session.commit = AsyncMock()
    monkeypatch.setattr(billing, "public_key_id", lambda: "rzp_test_key")
    monkeypatch.setattr(
        billing,
        "create_subscription",
        AsyncMock(
            return_value={
                "id": "sub_123",
                "status": "created",
                "short_url": "https://rzp.io/i/example",
            }
        ),
    )
    tenant_id = str(uuid.uuid4())

    response = await billing.create_billing_checkout(
        _request(),
        BillingCheckoutRequest(
            plan_tier="starter", billing_interval="monthly", currency="usd"
        ),
        session,
        tenant_id,
    )

    assert response.data.subscription_id == "sub_123"
    assert response.data.checkout_url == "https://rzp.io/i/example"
    persisted = session.add.call_args.args[0]
    assert isinstance(persisted, BillingSubscription)
    assert persisted.tenant_id == uuid.UUID(tenant_id)
    assert persisted.plan_tier == PlanTier.starter
    session.commit.assert_awaited_once()
    assert session.execute.await_args_list[0].args[0]._for_update_arg is not None


@pytest.mark.asyncio
async def test_checkout_rejects_second_active_subscription() -> None:
    active = BillingSubscription(
        tenant_id=uuid.uuid4(),
        provider_subscription_id="sub_active",
        plan_tier=PlanTier.starter,
        billing_interval="monthly",
        currency="usd",
        status="active",
    )
    session = MagicMock()
    session.execute = AsyncMock(
        side_effect=[_query_result(object()), _query_result(active)]
    )

    with pytest.raises(APIError) as exc_info:
        await billing.create_billing_checkout(
            _request(),
            BillingCheckoutRequest(
                plan_tier="growth", billing_interval="monthly", currency="usd"
            ),
            session,
            str(active.tenant_id),
        )

    assert exc_info.value.status_code == 409
    assert exc_info.value.error == "active_subscription_already_exists"


@pytest.mark.asyncio
async def test_checkout_does_not_call_provider_without_tenant_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = MagicMock()
    session.execute = AsyncMock(return_value=_query_result(None))
    create_provider_subscription = AsyncMock()
    monkeypatch.setattr(billing, "create_subscription", create_provider_subscription)

    with pytest.raises(APIError) as exc_info:
        await billing.create_billing_checkout(
            _request(),
            BillingCheckoutRequest(
                plan_tier="starter", billing_interval="monthly", currency="inr"
            ),
            session,
            str(uuid.uuid4()),
        )

    assert exc_info.value.status_code == 503
    assert exc_info.value.error == "tenant_billing_not_ready"
    create_provider_subscription.assert_not_awaited()


@pytest.mark.asyncio
async def test_checkout_verification_requires_tenant_owned_subscription(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    subscription = BillingSubscription(
        tenant_id=uuid.uuid4(),
        provider_subscription_id="sub_123",
        plan_tier=PlanTier.starter,
        billing_interval="monthly",
        currency="inr",
        status="created",
    )
    session = MagicMock()
    session.execute = AsyncMock(return_value=_query_result(subscription))
    session.commit = AsyncMock()
    verifier = MagicMock()
    monkeypatch.setattr(billing, "verify_checkout_signature", verifier)

    response = await billing.verify_billing_checkout(
        _request(),
        BillingCheckoutVerificationRequest(
            razorpay_payment_id="pay_123",
            razorpay_subscription_id="sub_123",
            razorpay_signature="signature",
        ),
        session,
        str(subscription.tenant_id),
    )

    assert response.data.verified is True
    assert response.data.plan_tier == "starter"
    verifier.assert_called_once_with(
        payment_id="pay_123", subscription_id="sub_123", signature="signature"
    )
    assert subscription.status == "authenticated"
    session.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_checkout_verification_rejects_unknown_subscription() -> None:
    session = MagicMock()
    session.execute = AsyncMock(return_value=_query_result(None))

    with pytest.raises(APIError) as exc_info:
        await billing.verify_billing_checkout(
            _request(),
            BillingCheckoutVerificationRequest(
                razorpay_payment_id="pay_123",
                razorpay_subscription_id="sub_other",
                razorpay_signature="signature",
            ),
            session,
            str(uuid.uuid4()),
        )

    assert exc_info.value.status_code == 404
