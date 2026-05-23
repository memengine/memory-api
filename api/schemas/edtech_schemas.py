from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from datetime import datetime
from typing import Any

from pydantic import BaseModel
from pydantic import Field

from api.schemas.responses import ResponseEnvelope


@dataclass(slots=True)
class EdTechExtractionResult:
    fields_updated: list[str]
    conflicts_resolved: int
    nothing_to_extract: bool
    tokens_used: int
    provider_used: str


@dataclass(slots=True)
class EdTechRetrieveResult:
    system_prompt_addition: str
    context_token_count: int
    days_to_exam: int | None = None


class EdTechMemoryView(BaseModel):
    id: str
    proxy_user_id: str
    tenant_id: str
    grade_level: str | None = None
    board_or_curriculum: str | None = None
    subjects: list[dict[str, Any]] = Field(default_factory=list)
    syllabus_stage: dict[str, Any] = Field(default_factory=dict)
    strong_topics: list[dict[str, Any]] = Field(default_factory=list)
    weak_topics: list[dict[str, Any]] = Field(default_factory=list)
    concept_gaps: list[dict[str, Any]] = Field(default_factory=list)
    misconceptions: list[dict[str, Any]] = Field(default_factory=list)
    explanation_style: dict[str, Any] | None = None
    session_profile: dict[str, Any] | None = None
    language_profile: dict[str, Any] | None = None
    peak_hours: dict[str, Any] | None = None
    exam_name: str | None = None
    exam_date: date | None = None
    marks_target: dict[str, Any] | None = None
    mock_scores: list[dict[str, Any]] = Field(default_factory=list)
    forgetting_stages: dict[str, Any] = Field(default_factory=dict)
    improvement_velocity: dict[str, Any] = Field(default_factory=dict)
    streak: dict[str, Any] | None = None
    last_topic_studied: str | None = None
    schema_version: int = 1
    last_extraction_at: datetime | None = None
    extraction_source_job_ids: list[str] = Field(default_factory=list)
    created_at: datetime | None = None
    updated_at: datetime | None = None


class EdTechProfileResponse(ResponseEnvelope):
    data: EdTechMemoryView | None


class EnableEdTechSchemaData(BaseModel):
    enabled: bool
    effective_from: str


class EnableEdTechSchemaResponse(ResponseEnvelope):
    data: EnableEdTechSchemaData


__all__ = [
    "EdTechExtractionResult",
    "EdTechMemoryView",
    "EdTechProfileResponse",
    "EdTechRetrieveResult",
    "EnableEdTechSchemaData",
    "EnableEdTechSchemaResponse",
]
