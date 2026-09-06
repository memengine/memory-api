from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel

PlanName = Literal["free", "starter", "growth", "scale", "enterprise"]
CtaType = Literal["signup", "checkout", "sales"]
OveragePolicy = Literal["block", "warn", "charge"]


class BillingPlanLimits(BaseModel):
    monthly_call_limit: int | None = None
    write_call_limit: int | None = None
    read_limit: int | None = None
    rate_limit_per_user_per_minute: int | None = None
    overage_policy: OveragePolicy | None = None
    overage_policy_label: str | None = None


class BillingPlanFeatures(BaseModel):
    quality_gate: bool
    domain_schemas: bool
    cross_agent: bool
    conflict_resolution: bool
    multi_service_writers: bool
    audit_log_days: int
    support: str
    reliability_note: str
    data_residency: str


class BillingPlan(BaseModel):
    name: PlanName
    display_name: str
    badge: str
    monthly_price_inr: int | None
    annual_price_inr: int | None
    monthly_price_usd: int | None
    annual_price_usd: int | None
    is_popular: bool
    cta_text: str
    cta_type: CtaType
    limits: BillingPlanLimits
    features: BillingPlanFeatures


BillingInterval = Literal["monthly", "annual"]
BillingCurrency = Literal["inr", "usd"]
PaidPlanName = Literal["starter", "growth", "scale"]


class BillingCheckoutRequest(BaseModel):
    plan_tier: PaidPlanName
    billing_interval: BillingInterval
    currency: BillingCurrency = "inr"


class BillingCheckoutData(BaseModel):
    provider: Literal["razorpay"] = "razorpay"
    key_id: str
    subscription_id: str
    checkout_url: str
    plan_tier: PaidPlanName
    billing_interval: BillingInterval
    currency: BillingCurrency


class BillingCheckoutResponse(BaseModel):
    data: BillingCheckoutData
    request_id: str
    timestamp: datetime


class BillingCheckoutVerificationRequest(BaseModel):
    razorpay_payment_id: str
    razorpay_subscription_id: str
    razorpay_signature: str


class BillingCheckoutVerificationData(BaseModel):
    verified: bool = True
    plan_tier: PaidPlanName


class BillingCheckoutVerificationResponse(BaseModel):
    data: BillingCheckoutVerificationData
    request_id: str
    timestamp: datetime


class BillingSubscriptionData(BaseModel):
    provider: Literal["razorpay"] | None = None
    provider_subscription_id: str | None = None
    plan_tier: PlanName
    billing_interval: BillingInterval | None = None
    currency: BillingCurrency | None = None
    status: str
    current_period_end: datetime | None = None
    cancel_at_period_end: bool = False
    limits: BillingPlanLimits


class BillingSubscriptionResponse(BaseModel):
    data: BillingSubscriptionData
    request_id: str
    timestamp: datetime
