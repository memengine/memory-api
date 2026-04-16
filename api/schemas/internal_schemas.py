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


class QueueStatus(BaseModel):
    name: str
    depth: int
    oldest_job_age_seconds: int | None = None
    threshold: int
    status: QueueHealthValue


class SystemHealthResponse(BaseModel):
    circuits: list[CircuitStatus]
    queues: list[QueueStatus]
    overall_status: OverallHealthValue
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


class CostSummaryResponse(BaseModel):
    total_tokens_mtd: int
    total_estimated_cost_usd: float
    top_5_tenants_by_cost: list[CostSummaryTenant]
    avg_cost_per_call: float | None = None
    total_gate_blocks_mtd: int
    estimated_savings_from_gate_usd: float
    projected_month_cost_usd: float
    cost_is_estimate: bool = True


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


class DeadLetterJob(BaseModel):
    id: str
    tenant_id: str
    proxy_user_id: str
    external_user_id: str | None = None
    status: str
    attempts: int = 0
    queue_name: str | None = None
    error: str | None = None
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
