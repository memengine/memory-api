from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel

from api.schemas.responses import ResponseEnvelope


class CrossUserConflictData(BaseModel):
    id: str
    tenant_id: str
    entity_type: str
    entity_value_a: str
    entity_value_b: str
    user_a_memory_id: str | None = None
    user_b_memory_id: str | None = None
    user_a_id: str | None = None
    user_b_id: str | None = None
    memory_a_content: str | None = None
    memory_b_content: str | None = None
    memory_a_created_at: datetime | None = None
    memory_b_created_at: datetime | None = None
    detected_at: datetime
    status: str
    resolved_at: datetime | None = None
    resolution: str | None = None
    resolution_path: str | None = None
    resolved_by: str | None = None
    resolution_reason: str | None = None
    auto_resolution: str | None = None
    auto_resolution_at: datetime | None = None
    requires_attention: bool = False


class CrossUserConflictsResponse(ResponseEnvelope):
    data: list[CrossUserConflictData]


class CrossUserConflictUpdateRequest(BaseModel):
    status: str
    correct_user: str | None = None


class TenantConflictResolveRequest(BaseModel):
    correct_user: str
    reason: str | None = None


class TenantConflictResolveData(BaseModel):
    resolved: bool
    conflict_id: str
    action_taken: str


class TenantConflictResolveResponse(ResponseEnvelope):
    data: TenantConflictResolveData


class ConflictStatsData(BaseModel):
    total_detected_mtd: int
    auto_resolved_mtd: int
    auto_resolution_rate: float
    resolution_breakdown: dict[str, int]
    requires_attention: int
    clarifications_pending: int
    pending_user_session: int = 0
    pending_tenant_review: int = 0
    resolved_by_user_session_mtd: int = 0
    resolved_by_tenant_mtd: int = 0


class ConflictStatsResponse(ResponseEnvelope):
    data: ConflictStatsData
