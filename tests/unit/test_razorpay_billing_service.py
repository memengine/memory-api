from __future__ import annotations

import hashlib
import hmac

import httpx
import pytest

from api.errors import APIError
from api.services.razorpay_billing_service import (
    create_subscription,
    verify_checkout_signature,
    verify_webhook_signature,
)


def test_verify_webhook_signature_uses_raw_body(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("RAZORPAY_WEBHOOK_SECRET", "webhook-secret")
    body = b'{"event":"subscription.activated"}'
    signature = hmac.new(b"webhook-secret", body, hashlib.sha256).hexdigest()

    verify_webhook_signature(body, signature)

    with pytest.raises(APIError) as exc_info:
        verify_webhook_signature(body + b" ", signature)
    assert exc_info.value.status_code == 401


@pytest.mark.asyncio
async def test_create_subscription_uses_configured_provider_plan(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("RAZORPAY_KEY_ID", "rzp_test_key")
    monkeypatch.setenv("RAZORPAY_KEY_SECRET", "test-secret")
    monkeypatch.setenv("RAZORPAY_PLAN_STARTER_MONTHLY_USD", "plan_starter_monthly_usd")
    captured: dict[str, object] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = request.content
        captured["authorization"] = request.headers.get("authorization")
        return httpx.Response(
            200, json={"id": "sub_123", "short_url": "https://rzp.io/i/example"}
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await create_subscription(
            tenant_id="00000000-0000-0000-0000-000000000001",
            plan_tier="starter",
            billing_interval="monthly",
            currency="usd",
            client=client,
        )

    assert result["id"] == "sub_123"
    assert b'"plan_id":"plan_starter_monthly_usd"' in captured["body"]
    assert str(captured["authorization"]).startswith("Basic ")


def test_verify_checkout_signature_binds_payment_to_subscription(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("RAZORPAY_KEY_ID", "rzp_test_key")
    monkeypatch.setenv("RAZORPAY_KEY_SECRET", "test-secret")
    signature = hmac.new(b"test-secret", b"pay_123|sub_123", hashlib.sha256).hexdigest()

    verify_checkout_signature(
        payment_id="pay_123", subscription_id="sub_123", signature=signature
    )

    with pytest.raises(APIError) as exc_info:
        verify_checkout_signature(
            payment_id="pay_123", subscription_id="sub_other", signature=signature
        )
    assert exc_info.value.status_code == 401
