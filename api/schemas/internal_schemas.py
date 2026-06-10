from __future__ import annotations

import uuid
from datetime import date
from datetime import datetime
from typing import Literal

from pydantic import BaseModel
from pydantic import Field

from api.schemas.tenant_schemas import TenantUsageData


CircuitStateValue = Literal["CLOSED", "OPEN", "HALF_OPEN"]
QueueHealthValue = Literal["NORMAL", "BACKLOG", "CRITICAL"]
OverallHealthValue = Literal["HEALTHY", "DEGRADED", "CRITICAL"]
QuotaModeValue = Literal["FULL", "PASSTHROUGH", "DEGRADED_RETRIEVE", "BLOCKED"]
PlanTierValue = Literal["free", "starter", "growth", "enterprise"]
BlockedLayerValue = Literal["L1", "L2", "L3", "L4"]
OveragePolicyValue = Literal["block", "warn", "charge"]


class CircuitStatus(BaseModel):
    name: str
    state: CircuitStateValue
    open_since: datetime | None = None
    failure_count: int = 0


class LLMProviderStatus(BaseModel):
    name: str
    state: CircuitStateValue
    failures: int = 0
    configured: bool = True
    last_failure_at: datetime | None = None


class QueueStatus(BaseModel):
    name: str
    depth: int
    oldest_job_age_seconds: int | None = None
    threshold: int
    status: QueueHealthValue


class SystemHealthResponse(BaseModel):
    circuits: list[CircuitStatus]
    llm_providers: list[LLMProviderStatus] = Field(default_factory=list)
    queues: list[QueueStatus]
    overall_status: OverallHealthValue
    generated_at: datetime


class ProviderUsageData(BaseModel):
    last_hour: dict[str, int] = Field(default_factory=dict)
    active_provider: str | None = None


class ProviderUsageResponse(BaseModel):
    data: ProviderUsageData
    generated_at: datetime


class TenantSummary(BaseModel):
    tenant_id: str
    company_name: str
    plan_tier: PlanTierValue
    quota_mode: QuotaModeValue
    quota_pct: float
    memory_count: int
    active_users_7d: int
    dead_job_count: int
    last_api_call: datetime | None = None
    needs_attention: bool
    extraction_success_rate: float | None = None
    nothing_to_extract_rate: float | None = None
    avg_extraction_tokens: float | None = None
    total_extraction_calls_mtd: int = 0
    hot_memories_count: int = 0
    requires_attention: int = 0
    clarifications_pending: int = 0
    auto_resolution_rate: float | None = None


class AllTenantsResponse(BaseModel):
    tenants: list[TenantSummary]
    next_cursor: str | None = None
    limit: int
    generated_at: datetime


class InternalTenantRecord(BaseModel):
    tenant_id: str
    company_name: str
    plan_tier: PlanTierValue
    created_at: datetime


class RecentExtractionJob(BaseModel):
    id: str
    status: str
    proxy_user_id: str
    created_at: datetime | None = None
    processing_started_at: datetime | None = None
    completed_at: datetime | None = None
    attempts: int = 0
    error: str | None = None


class QualitySummary(BaseModel):
    total_calls: int
    blocked_calls: int
    block_rate: float
    by_layer: dict[BlockedLayerValue, int]


class TenantDetail(BaseModel):
    tenant: InternalTenantRecord
    usage: TenantUsageData
    recent_jobs: list[RecentExtractionJob] = Field(default_factory=list)
    quality_summary: QualitySummary
    cost_estimate_mtd: float
    cost_is_estimate: bool = True


class CostSummaryTenant(BaseModel):
    tenant_id: uuid.UUID
    company_name: str
    tokens: int
    estimated_cost_usd: float
    extraction_success_rate: float | None = None
    conflicts_resolved_mtd: int = 0
    nothing_to_extract_rate: float | None = None
    add_calls: int = 0


class NothingToExtractTenant(BaseModel):
    tenant_id: uuid.UUID
    company_name: str
    rate: float
    add_calls: int


class CostSummaryResponse(BaseModel):
    total_tokens_mtd: int
    total_estimated_cost_usd: float
    top_5_tenants_by_cost: list[CostSummaryTenant]
    avg_cost_per_call: float | None = None
    total_gate_blocks_mtd: int
    estimated_savings_from_gate_usd: float
    projected_month_cost_usd: float
    cost_is_estimate: bool = True
    avg_extraction_tokens: float = 0.0
    total_extraction_calls_mtd: int = 0
    extraction_success_rate: float = 0.0
    nothing_to_extract_rate: float = 0.0
    top_5_by_nothing_to_extract: list[NothingToExtractTenant] = Field(default_factory=list)
    conflicts_resolved_mtd: int = 0
    memories_auto_archived_mtd: int = 0


class SystemCostSummary(CostSummaryResponse):
    pass


class BackfillJobResponse(BaseModel):
    id: uuid.UUID
    task_name: str
    status: str
    total_rows: int | None = None
    processed_rows: int
    pct_complete: float | None = None
    started_at: datetime
    completed_at: datetime | None = None
    error: str | None = None
    eta_seconds: int | None = None


class AuditLogEntry(BaseModel):
    id: uuid.UUID
    tenant_id: uuid.UUID | None = None
    company_name: str | None = None
    action: str
    memory_id: uuid.UUID | None = None
    created_at: datetime
    ip_address: str | None = None
    old_value_summary: str | None = None
    new_value_summary: str | None = None
    metadata: dict | None = None


class AuditLogsResponse(BaseModel):
    data: list[AuditLogEntry]
    next_cursor: str | None = None
    total_count: int
    start_date: date
    end_date: date


class GlobalAgentVerificationRecord(BaseModel):
    id: uuid.UUID
    owner_tenant_id: uuid.UUID
    owner_tenant_name: str
    name: str
    description: str | None = None
    website_url: str | None = None
    logo_url: str | None = None
    default_categories_requested: list[str] = Field(default_factory=list)
    is_verified: bool
    is_public: bool
    is_active: bool
    grants_count: int = 0
    created_at: datetime


class GlobalAgentVerificationResponse(BaseModel):
    data: list[GlobalAgentVerificationRecord]
    generated_at: datetime


class GlobalAgentVerificationUpdateRequest(BaseModel):
    is_verified: bool


class DeadLetterJob(BaseModel):
    id: str
    tenant_id: str
    proxy_user_id: str
    external_user_id: str | None = None
    status: str
    attempts: int = 0
    queue_name: str | None = None
    error: str | None = None
    error_type: str | None = None
    payload: dict | None = None
    queued_at: datetime | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None
    dead_lettered_at: datetime | None = None


class DeadLetterDiscardResponse(BaseModel):
    discarded: bool
    job_id: str


class PlanChangeRequest(BaseModel):
    plan_tier: PlanTierValue


class TenantBudgetRecord(BaseModel):
    id: str
    tenant_id: str
    plan_tier: PlanTierValue
    monthly_call_limit: int | None = None
    monthly_token_limit: int | None = None
    current_month_calls: int
    current_month_tokens: int
    rate_limit_per_user_per_minute: int | None = None
    overage_policy: OveragePolicyValue
    alert_threshold_pct: float
    reset_at: datetime | None = None
    created_at: datetime
    write_calls: int
    write_call_limit: int | None = None
    read_calls: int
    read_limit: int | None = None
    last_notified_mode: QuotaModeValue | None = None
    last_notified_pct: float | None = None
    alert_webhook_url: str | None = None
    webhook_secret: str | None = None
