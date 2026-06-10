from __future__ import annotations

from datetime import date
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
DomainSchemaValue = Literal["edtech", "support"] | None
DomainStatusValue = Literal["available", "coming_soon"]


class TenantUsageData(BaseModel):
    calls_used: int
    calls_limit: int | None = None
    tokens_used: int
    tokens_limit: int | None = None
    mode: QuotaModeValue
    budget_remaining_pct: float
    reset_at: datetime | None = None
    plan_tier: PlanTierValue
    conflicts_resolved_mtd: int = 0
    extraction_success_rate: float = 0.0
    nothing_to_extract_rate: float = 0.0
    cross_user_conflicts_pending: int = 0
    conflict_types_breakdown: dict[str, int] = Field(default_factory=dict)


class TenantUsageResponse(ResponseEnvelope):
    data: TenantUsageData


class TenantProxyUserData(BaseModel):
    external_user_id: str
    memory_count: int
    last_active_at: datetime | None = None
    created_at: datetime | None = None
    is_blocked: bool = False
    quality_score_avg: float | None = None


class TenantUsersListResponse(ResponseEnvelope):
    data: list[TenantProxyUserData]
    pagination: CursorPage


class TenantMemoryAdditionPoint(BaseModel):
    day: datetime
    count: int


class TenantMemoryAdditionsResponse(ResponseEnvelope):
    data: list[TenantMemoryAdditionPoint]


class AvailableDomain(BaseModel):
    value: str | None = None
    label: str
    description: str
    status: DomainStatusValue


class TenantDomainSchemaData(BaseModel):
    domain_schema: DomainSchemaValue = None
    available_domains: list[AvailableDomain] = Field(default_factory=list)
    support_type_configured: str | None = None
    support_type_mode: str = "single"
    support_types_allowed: list[str] = Field(default_factory=list)


class TenantDomainSchemaPatchRequest(BaseModel):
    domain_schema: DomainSchemaValue = None


class TenantDomainSchemaResponse(ResponseEnvelope):
    data: TenantDomainSchemaData


class StudentSummary(BaseModel):
    external_user_id: str
    grade_level: str | None = None
    board_or_curriculum: str | None = None
    exam_name: str | None = None
    exam_date: date | None = None
    days_to_exam: int | None = None
    weak_topics_count: int = 0
    forgetting_risk_count: int = 0
    last_session_at: datetime | None = None


class TenantStudentsResponse(ResponseEnvelope):
    data: list[StudentSummary]
    pagination: CursorPage


class BlockEvent(BaseModel):
    blocked_at: datetime
    layer: BlockedLayerValue
    reason: str | None = None


class ProxyUserDetail(BaseModel):
    external_user_id: str
    user_id: str | None = None
    memory_count: int
    last_active_at: datetime | None = None
    created_at: datetime | None = None
    quality_score_avg: float | None = None
    block_history: list[BlockEvent] = Field(default_factory=list)
    total_calls_7d: int = 0
    blocked_calls_7d: int = 0


class ProxyUserDetailResponse(ResponseEnvelope):
    data: ProxyUserDetail


class TenantQualityLogEntry(BaseModel):
    id: str
    external_user_id: str
    layer_blocked_at: BlockedLayerValue
    reason: str | None = None
    nothing_to_extract: bool = False
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


class CostSummary(BaseModel):
    current_month_tokens: int
    estimated_cost_usd: float
    cost_per_call: float | None = None
    gate_block_rate: float
    projected_month_cost_usd: float
    savings_from_gate_usd: float
    cost_is_estimate: bool = True


class CostSummaryResponse(ResponseEnvelope):
    data: CostSummary
