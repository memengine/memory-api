from __future__ import annotations

import uuid
from datetime import UTC
from datetime import date
from datetime import datetime
from typing import Any
from typing import Literal

from pydantic import BaseModel
from pydantic import AliasChoices
from pydantic import Field
from pydantic import field_validator


MemoryCategory = Literal[
    "preference",
    "fact",
    "goal",
    "procedure",
    "relationship",
    "expertise",
]

MessageRole = Literal["user", "assistant", "system"]
QuotaMode = Literal["FULL", "PASSTHROUGH", "DEGRADED_RETRIEVE", "BLOCKED"]
CircuitStatus = Literal["HEALTHY", "DEGRADED", "CRITICAL"]
ProcessingStatus = Literal["normal", "delayed"]
RetrievalFeedbackOutcome = Literal[
    "used_successfully",
    "used_partially",
    "ignored",
    "not_useful",
    "user_corrected",
    "clarification_needed",
]


class ConversationMessage(BaseModel):
    role: MessageRole
    content: str = Field(min_length=1)

    @field_validator("content")
    @classmethod
    def validate_content(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("Message content must not be empty.")
        return stripped


class EvidenceReference(BaseModel):
    source_type: str = Field(min_length=1, max_length=50)
    reference: str = Field(min_length=1, max_length=500)
    content_hash: str | None = Field(default=None, min_length=64, max_length=64)


class MemorySource(BaseModel):
    event_id: str = Field(min_length=1, max_length=255)
    service: str = Field(min_length=1, max_length=100, pattern=r"^[a-z0-9][a-z0-9._-]*$")
    observed_at: datetime
    scope: dict[str, Any] = Field(default_factory=dict)
    evidence: list[EvidenceReference] = Field(default_factory=list, max_length=20)

    @classmethod
    def for_service(
        cls,
        service: str,
        *,
        event_id: str | None = None,
        observed_at: datetime | None = None,
        scope: dict[str, Any] | None = None,
        evidence: list[EvidenceReference | dict[str, Any]] | None = None,
    ) -> "MemorySource":
        """Build source metadata without making teams manage IDs on day one.

        Use this when more than one backend service writes memory. Solo apps can
        omit source entirely and MemoryOS will generate a default source event.
        """
        return cls(
            event_id=event_id or f"sdk-{uuid.uuid4()}",
            service=service,
            observed_at=observed_at or datetime.now(UTC),
            scope=scope or {},
            evidence=[
                item if isinstance(item, EvidenceReference) else EvidenceReference.model_validate(item)
                for item in (evidence or [])
            ],
        )


class AddRequest(BaseModel):
    external_user_id: str = Field(min_length=1)
    agent_id: str | None = None
    messages: list[ConversationMessage] = Field(min_length=1)
    metadata: dict[str, Any] = Field(default_factory=dict)
    idempotency_key: str | None = None
    source: MemorySource | None = None


class AddResult(BaseModel):
    job_id: str | None = None
    status: str
    blocked_reason: str | None = None
    retry_after_seconds: int | None = None
    budget_remaining_pct: float | None = None
    quota_mode: QuotaMode = "FULL"
    processing_eta_seconds: int | None = None
    processing_status: ProcessingStatus = "normal"
    circuit_status: CircuitStatus = "HEALTHY"
    nothing_to_extract: bool = False

    @property
    def was_stored(self) -> bool:
        return self.status == "queued" and not self.nothing_to_extract


class MemoryResult(BaseModel):
    id: str
    content: str
    category: MemoryCategory
    importance_score: float
    last_accessed: datetime | None = None
    relevance_score: float
    context_snippet: str
    access_count: int = 0
    original_importance_score: float = 0.0
    is_hot: bool = False
    system_archived: bool = False
    source_event_id: str | None = None
    provenance: dict[str, Any] | None = None

    @property
    def importance_delta(self) -> float:
        return round(float(self.importance_score) - float(self.original_importance_score), 2)

    @property
    def importance_trend(self) -> str:
        delta = self.importance_delta
        if delta > 0.3:
            return "rising"
        if delta < -0.3:
            return "decaying"
        return "stable"


class RetrieveResult(BaseModel):
    retrieval_id: str | None = None
    items: list[MemoryResult] = Field(default_factory=list)
    cached: bool = False
    system_prompt_addition: str = ""
    context_token_count: int = 0
    memories_from_hot_tier: int = 0
    quota_mode: QuotaMode = "FULL"
    is_passthrough: bool = False
    is_degraded: bool = False
    circuit_status: CircuitStatus = "HEALTHY"

    @property
    def has_context(self) -> bool:
        return bool(self.system_prompt_addition) and not self.is_passthrough

    def __iter__(self):
        return iter(self.items)

    def __len__(self) -> int:
        return len(self.items)

    def __getitem__(self, index: int) -> MemoryResult:
        return self.items[index]


class RetrievalFeedbackRequest(BaseModel):
    retrieval_id: str = Field(min_length=1)
    outcome: RetrievalFeedbackOutcome
    used_memory_ids: list[str] = Field(default_factory=list, max_length=50)
    correction: str | None = Field(default=None, min_length=1, max_length=4000)
    agent_confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    metadata: dict[str, Any] = Field(default_factory=dict)


class RetrievalFeedbackResult(BaseModel):
    feedback_id: str
    retrieval_id: str
    outcome: str
    correction_job_id: str | None = None

    @property
    def queued_retrospective_extraction(self) -> bool:
        return self.correction_job_id is not None


class MemoryRecord(BaseModel):
    id: str
    content: str
    category: MemoryCategory
    importance_score: float
    confidence_score: float = Field(default=0.0, validation_alias=AliasChoices("confidence_score", "confidence"))
    created_at: datetime | None = None
    updated_at: datetime | None = None
    last_accessed_at: datetime | None = None
    access_count: int = 0
    original_importance_score: float = 0.0
    is_hot: bool = False
    system_archived: bool = False
    is_archived: bool = False
    agent_id: str | None = None
    previous_version_id: str | None = None
    source_conversation_id: str | None = None
    source_event_id: str | None = None
    provenance: dict[str, Any] | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    @property
    def importance_delta(self) -> float:
        return round(float(self.importance_score) - float(self.original_importance_score), 2)

    @property
    def importance_trend(self) -> str:
        delta = self.importance_delta
        if delta > 0.3:
            return "rising"
        if delta < -0.3:
            return "decaying"
        return "stable"


Memory = MemoryRecord


class CursorPageInfo(BaseModel):
    next_cursor: str | None = None
    limit: int
    total: int


class MemoryPage(BaseModel):
    items: list[MemoryRecord]
    next_cursor: str | None = None
    limit: int
    total: int


class UserProfile(BaseModel):
    id: str
    external_id: str
    email: str
    settings: dict[str, Any]
    memory_count: int
    storage_bytes: int


class ApiKey(BaseModel):
    id: str
    name: str
    permissions: list[str]
    rate_limit_per_minute: int
    created_at: datetime | None = None
    last_used_at: datetime | None = None
    is_active: bool


class Agent(BaseModel):
    id: str
    name: str
    description: str | None = None
    memory_scope: Literal["private", "shared"]
    created_at: datetime | None = None


class MemoryExport(BaseModel):
    user: UserProfile
    memories: list[MemoryRecord]
    api_keys: list[ApiKey]
    agents: list[Agent]


class EdTechMemoryProfile(BaseModel):
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

    @property
    def has_exam_context(self) -> bool:
        return bool(self.exam_name or self.exam_date or self.marks_target)

    @property
    def has_learning_profile(self) -> bool:
        return bool(self.explanation_style or self.language_profile or self.session_profile)


class DeleteResult(BaseModel):
    deleted: bool


class ErrorPayload(BaseModel):
    error: str
    code: str
    request_id: str
    details: Any | None = None


class EnvelopeBase(BaseModel):
    request_id: str
    timestamp: datetime


class AddEnvelope(EnvelopeBase):
    job_id: str | None = None
    status: str
    blocked_reason: str | None = None
    retry_after_seconds: int | None = None
    budget_remaining_pct: float | None = None
    processing_eta_seconds: int | None = None
    processing_status: ProcessingStatus = "normal"
    nothing_to_extract: bool = False


class RetrieveEnvelope(EnvelopeBase):
    retrieval_id: str | None = None
    data: list[MemoryResult]
    cached: bool
    system_prompt_addition: str
    context_token_count: int = 0
    memories_from_hot_tier: int = 0
    quota_mode: QuotaMode = "FULL"


class RetrievalFeedbackEnvelope(EnvelopeBase):
    data: RetrievalFeedbackResult


class MemoryListEnvelope(EnvelopeBase):
    data: list[MemoryRecord]
    pagination: CursorPageInfo


class ExportEnvelope(EnvelopeBase):
    data: MemoryExport


class DeleteEnvelope(EnvelopeBase):
    data: DeleteResult


class EdTechProfileEnvelope(EnvelopeBase):
    data: EdTechMemoryProfile | None
