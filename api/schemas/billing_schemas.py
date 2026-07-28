from __future__ import annotations

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
    stripe_price_monthly: str | None = None
    stripe_price_annual: str | None = None
    limits: BillingPlanLimits
    features: BillingPlanFeatures
