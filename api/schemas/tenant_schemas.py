from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel
from pydantic import Field

from api.schemas.responses import CursorPage
from api.schemas.responses import ResponseEnvelope


OveragePolicyValue = Literal["block", "warn", "charge"]
QuotaModeValue = Literal["FULL", "PASSTHROUGH", "DEGRADED_RETRIEVE", "BLOCKED"]
PlanTierValue = Literal["free", "starter", "growth", "enterprise"]
BlockedLayerValue = Literal["L1", "L2", "L3", "L4", "NONE"]


class TenantUsageData(BaseModel):
    calls_used: int
    calls_limit: int | None = None
    tokens_used: int
    tokens_limit: int | None = None
    mode: QuotaModeValue
    budget_remaining_pct: float
    reset_at: datetime | None = None
    plan_tier: PlanTierValue


class TenantUsageResponse(ResponseEnvelope):
    data: TenantUsageData


class TenantProxyUserData(BaseModel):
    external_user_id: str
    memory_count: int
    last_active_at: datetime | None = None
    created_at: datetime | None = None
    is_blocked: bool = False


class TenantUsersListResponse(ResponseEnvelope):
    data: list[TenantProxyUserData]
    pagination: CursorPage


class TenantQualityLogEntry(BaseModel):
    id: str
    external_user_id: str
    layer_blocked_at: BlockedLayerValue
    quality_score: float
    semantic_similarity: float | None = None
    created_at: datetime


class TenantQualityLogResponse(ResponseEnvelope):
    data: list[TenantQualityLogEntry]
    pagination: CursorPage


class TenantSettingsPatchRequest(BaseModel):
    alert_webhook_url: str | None = Field(default=None, max_length=500)
    overage_policy: OveragePolicyValue | None = None


class TenantSettingsData(BaseModel):
    alert_webhook_url: str | None = None
    overage_policy: OveragePolicyValue


class TenantSettingsResponse(ResponseEnvelope):
    data: TenantSettingsData


class TenantTestWebhookData(BaseModel):
    delivered: bool
    status_code: int


class TenantTestWebhookResponse(ResponseEnvelope):
    data: TenantTestWebhookData


class TenantDeprecationUsageEntry(BaseModel):
    field: str
    last_used: datetime
    sunset_at: datetime
    migration_guide: str
    replacement_field: str | None = None


class TenantDeprecationUsageResponse(ResponseEnvelope):
    data: list[TenantDeprecationUsageEntry]
