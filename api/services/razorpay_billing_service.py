from __future__ import annotations

import hashlib
import hmac
import os
from datetime import UTC, datetime
from typing import Any

import httpx

from api.errors import APIError

RAZORPAY_API_BASE = "https://api.razorpay.com/v1"
PAID_PLAN_NAMES = {"starter", "growth", "scale"}


def configured_plan_id(plan_tier: str, billing_interval: str, currency: str) -> str:
    if plan_tier not in PAID_PLAN_NAMES:
        raise APIError(status_code=422, code="BILL_422", error="paid_plan_required")
    env_name = f"RAZORPAY_PLAN_{plan_tier.upper()}_{billing_interval.upper()}_{currency.upper()}"
    value = os.getenv(env_name, "").strip()
    if not value:
        raise APIError(
            status_code=503, code="BILL_503", error="billing_plan_not_configured"
        )
    return value


def public_key_id() -> str:
    value = os.getenv("RAZORPAY_KEY_ID", "").strip()
    if not value:
        raise APIError(
            status_code=503, code="BILL_503", error="billing_provider_not_configured"
        )
    return value


def _provider_credentials() -> tuple[str, str]:
    key_id = public_key_id()
    key_secret = os.getenv("RAZORPAY_KEY_SECRET", "").strip()
    if not key_secret:
        raise APIError(
            status_code=503, code="BILL_503", error="billing_provider_not_configured"
        )
    return key_id, key_secret


async def create_subscription(
    *,
    tenant_id: str,
    plan_tier: str,
    billing_interval: str,
    currency: str,
    client: httpx.AsyncClient | None = None,
) -> dict[str, Any]:
    key_id, key_secret = _provider_credentials()
    plan_id = configured_plan_id(plan_tier, billing_interval, currency)
    owns_client = client is None
    http_client = client or httpx.AsyncClient(timeout=15.0)
    try:
        response = await http_client.post(
            f"{RAZORPAY_API_BASE}/subscriptions",
            auth=(key_id, key_secret),
            json={
                "plan_id": plan_id,
                "total_count": 1200 if billing_interval == "monthly" else 100,
                "quantity": 1,
                "customer_notify": True,
                "notes": {
                    "memoryos_tenant_id": tenant_id,
                    "memoryos_plan_tier": plan_tier,
                    "memoryos_billing_interval": billing_interval,
                    "memoryos_currency": currency,
                },
            },
        )
    except httpx.HTTPError as exc:
        raise APIError(
            status_code=502, code="BILL_502", error="billing_provider_unavailable"
        ) from exc
    finally:
        if owns_client:
            await http_client.aclose()
    if response.status_code not in {200, 201}:
        raise APIError(
            status_code=502, code="BILL_502", error="subscription_creation_failed"
        )
    payload = response.json()
    if not payload.get("id") or not payload.get("short_url"):
        raise APIError(
            status_code=502, code="BILL_502", error="invalid_billing_provider_response"
        )
    return payload


def verify_webhook_signature(raw_body: bytes, signature: str | None) -> None:
    secret = os.getenv("RAZORPAY_WEBHOOK_SECRET", "").strip()
    if not secret:
        raise APIError(
            status_code=503, code="BILL_503", error="billing_webhook_not_configured"
        )
    if not signature:
        raise APIError(
            status_code=401, code="BILL_401", error="missing_webhook_signature"
        )
    expected = hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, signature):
        raise APIError(
            status_code=401, code="BILL_401", error="invalid_webhook_signature"
        )


def verify_checkout_signature(
    *, payment_id: str, subscription_id: str, signature: str
) -> None:
    _, key_secret = _provider_credentials()
    message = f"{payment_id}|{subscription_id}".encode()
    expected = hmac.new(key_secret.encode("utf-8"), message, hashlib.sha256).hexdigest()
    if not signature or not hmac.compare_digest(expected, signature):
        raise APIError(
            status_code=401, code="BILL_401", error="invalid_checkout_signature"
        )


def webhook_event_id(raw_body: bytes, header_value: str | None) -> str:
    return (
        header_value.strip()
        if header_value and header_value.strip()
        else hashlib.sha256(raw_body).hexdigest()
    )


def unix_timestamp(value: object) -> datetime | None:
    if not isinstance(value, (int, float)) or value <= 0:
        return None
    return datetime.fromtimestamp(value, tz=UTC)
