from __future__ import annotations

import hashlib
import hmac
import json
import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest
from starlette.requests import Request

from api.db.models import BillingSubscription, PlanTier
from api.errors import APIError
from api.routers import razorpay_webhooks


def _request(payload: dict[str, object], secret: str = "webhook-secret") -> Request:
    body = json.dumps(payload, separators=(",", ":")).encode()
    signature = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    sent = False

    async def receive() -> dict[str, object]:
        nonlocal sent
        if sent:
            return {"type": "http.request", "body": b"", "more_body": False}
        sent = True
        return {"type": "http.request", "body": body, "more_body": False}

    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/v1/webhooks/razorpay",
            "headers": [
                (b"x-razorpay-signature", signature.encode()),
                (b"x-razorpay-event-id", b"evt_123"),
            ],
        },
        receive,
    )


def _subscription() -> BillingSubscription:
    return BillingSubscription(
        tenant_id=uuid.uuid4(),
        provider_subscription_id="sub_123",
        plan_tier=PlanTier.starter,
        billing_interval="monthly",
        currency="usd",
        status="created",
        checkout_url="https://rzp.io/i/example",
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("event_type", "provider_status"),
    [("subscription.activated", "active"), ("subscription.cancelled", "cancelled")],
)
async def test_webhook_updates_subscription_and_tenant_plan(
    monkeypatch: pytest.MonkeyPatch,
    event_type: str,
    provider_status: str,
) -> None:
    monkeypatch.setenv("RAZORPAY_WEBHOOK_SECRET", "webhook-secret")
    monkeypatch.setenv("RAZORPAY_PLAN_STARTER_MONTHLY_USD", "plan_starter_usd")
    monkeypatch.setattr(razorpay_webhooks, "invalidate_plan_cache", MagicMock())
    subscription = _subscription()
    result = MagicMock()
    result.scalar_one_or_none.return_value = subscription
    session = MagicMock()
    session.get = AsyncMock(return_value=None)
    session.flush = AsyncMock()
    newer_subscription = MagicMock()
    newer_subscription.scalar_one_or_none.return_value = None
    session.execute = AsyncMock(
        side_effect=[result, newer_subscription, MagicMock(), MagicMock()]
    )
    session.commit = AsyncMock()
    session.rollback = AsyncMock()
    payload = {
        "event": event_type,
        "payload": {
            "subscription": {
                "entity": {
                    "id": "sub_123",
                    "plan_id": "plan_starter_usd",
                    "status": provider_status,
                    "current_start": 1_700_000_000,
                    "current_end": 1_702_592_000,
                }
            }
        },
    }

    response = await razorpay_webhooks.receive_razorpay_webhook(
        _request(payload), session
    )

    assert response == {"received": True}
    assert subscription.status == provider_status
    assert session.execute.await_count == 4
    session.commit.assert_awaited_once()
    razorpay_webhooks.invalidate_plan_cache.assert_called_once_with(
        str(subscription.tenant_id)
    )


@pytest.mark.asyncio
async def test_delayed_terminal_webhook_cannot_downgrade_newer_subscription(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("RAZORPAY_WEBHOOK_SECRET", "webhook-secret")
    monkeypatch.setenv("RAZORPAY_PLAN_STARTER_MONTHLY_USD", "plan_starter_usd")
    subscription = _subscription()
    provider_lookup = MagicMock()
    provider_lookup.scalar_one_or_none.return_value = subscription
    newer_subscription_lookup = MagicMock()
    newer_subscription_lookup.scalar_one_or_none.return_value = object()
    session = MagicMock()
    session.get = AsyncMock(return_value=None)
    session.flush = AsyncMock()
    session.execute = AsyncMock(
        side_effect=[provider_lookup, newer_subscription_lookup]
    )
    session.commit = AsyncMock()

    payload = {
        "event": "subscription.cancelled",
        "payload": {
            "subscription": {
                "entity": {
                    "id": "sub_123",
                    "plan_id": "plan_starter_usd",
                    "status": "cancelled",
                }
            }
        },
    }

    response = await razorpay_webhooks.receive_razorpay_webhook(
        _request(payload), session
    )

    assert response == {"received": True}
    assert subscription.status == "cancelled"
    # Provider lookup plus the newer-subscription guard; no tenant downgrade.
    assert session.execute.await_count == 2
    session.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_duplicate_webhook_returns_without_reprocessing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("RAZORPAY_WEBHOOK_SECRET", "webhook-secret")
    session = MagicMock()
    session.get = AsyncMock(return_value=object())
    session.execute = AsyncMock()
    session.commit = AsyncMock()

    response = await razorpay_webhooks.receive_razorpay_webhook(
        _request({"event": "subscription.activated"}), session
    )

    assert response == {"received": True}
    session.execute.assert_not_awaited()
    session.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_invalid_signature_is_rejected_before_database(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("RAZORPAY_WEBHOOK_SECRET", "different-secret")
    session = MagicMock()
    session.get = AsyncMock()

    with pytest.raises(APIError) as exc_info:
        await razorpay_webhooks.receive_razorpay_webhook(
            _request({"event": "subscription.activated"}), session
        )

    assert exc_info.value.status_code == 401
    session.get.assert_not_awaited()


@pytest.mark.asyncio
async def test_unknown_subscription_is_retriable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("RAZORPAY_WEBHOOK_SECRET", "webhook-secret")
    session = MagicMock()
    session.get = AsyncMock(return_value=None)
    session.flush = AsyncMock()
    session.execute = AsyncMock(return_value=(_query_result := MagicMock()))
    _query_result.scalar_one_or_none.return_value = None
    session.rollback = AsyncMock()
    payload = {
        "event": "subscription.activated",
        "payload": {"subscription": {"entity": {"id": "sub_arrived_early"}}},
    }

    with pytest.raises(APIError) as exc_info:
        await razorpay_webhooks.receive_razorpay_webhook(_request(payload), session)

    assert exc_info.value.status_code == 503
    assert exc_info.value.error == "subscription_not_ready"
    session.rollback.assert_awaited_once()
