"""Explicit operator decisions; none of these fields are inferred from chat."""
from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from api.schemas.responses import ResponseEnvelope


class ServiceCorrectionItem(BaseModel):
    id: UUID
    content: str
    category: str
    created_at: datetime


class ServiceCorrectionListResponse(ResponseEnvelope):
    data: list[ServiceCorrectionItem]
    next_cursor: UUID | None = None


class ServiceCorrectionPreview(BaseModel):
    candidate_id: UUID
    candidate_content: str
    category: str
    scope: dict[str, Any]
    source_event_id: UUID
    source_service: str
    source_external_event_id: str
    observed_at: datetime
    target_memory_id: UUID
    target_content: str
    target_version: str
    claim_id: UUID
    winning_revision_id: UUID
    incoming_priority: int
    target_priority: int
    authority_basis: Literal["current_writer_rules"] = "current_writer_rules"
    review_version: str
    actions: list[Literal["apply", "decline"]]
    apply_blockers: list[str]


class ServiceCorrectionPreviewResponse(ResponseEnvelope):
    data: ServiceCorrectionPreview


class ServiceCorrectionDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")
    external_user_id: str = Field(min_length=1, max_length=255)
    target_memory_id: UUID
    target_version: str = Field(pattern=r"^[a-f0-9]{64}$")
    review_version: str = Field(pattern=r"^[a-f0-9]{64}$")
    action: Literal["apply", "decline"]


class ServiceCorrectionResult(BaseModel):
    candidate_id: UUID
    action: Literal["apply", "decline"]
    candidate_status: Literal["promoted", "dismissed"]
    memory_id: UUID
    claim_id: UUID
    winning_revision_id: UUID
    indexing_state: Literal["pending", "unchanged"]
    replayed: bool = False


class ServiceCorrectionDecisionResponse(ResponseEnvelope):
    data: ServiceCorrectionResult
