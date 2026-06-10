from __future__ import annotations

from datetime import datetime
from typing import Any
from typing import Literal

from pydantic import BaseModel
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
MemoryExpiry = Literal["permanent", "temporary"]
MemoryScope = Literal["private", "shared"]


class ExtractedMemorySchema(BaseModel):
    content: str = Field(min_length=1)
    category: MemoryCategory
    importance_score: float = Field(ge=1.0, le=10.0)
    confidence: float = Field(ge=0.0, le=1.0)
    expiry: MemoryExpiry
    reasoning: str = Field(min_length=1)

    @field_validator("content", "reasoning")
    @classmethod
    def strip_non_empty_strings(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("Value must not be empty.")
        return stripped


class ExtractionResponseSchema(BaseModel):
    memories: list[ExtractedMemorySchema] = Field(default_factory=list)


class ErrorResponse(BaseModel):
    error: str
    code: str
    request_id: str
    details: Any | None = None


class CursorPage(BaseModel):
    next_cursor: str | None = None
    limit: int
    total: int


class ResponseEnvelope(BaseModel):
    request_id: str
    timestamp: datetime


class MemoryData(BaseModel):
    id: str
    content: str
    category: MemoryCategory
    importance_score: float
    confidence_score: float
    created_at: datetime | None = None
    updated_at: datetime | None = None
    last_accessed_at: datetime | None = None
    access_count: int = 0
    is_archived: bool = False
    agent_id: str | None = None
    previous_version_id: str | None = None
    source_conversation_id: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class MemorySearchResult(BaseModel):
    id: str
    content: str
    category: MemoryCategory
    importance_score: float
    last_accessed: datetime | None = None
    relevance_score: float
    context_snippet: str


class MemoryListResponse(ResponseEnvelope):
    data: list[MemoryData]
    pagination: CursorPage


class MemoryGetResponse(ResponseEnvelope):
    data: MemoryData


class MemoryMutationResponse(ResponseEnvelope):
    data: MemoryData


class MemoryDeleteData(BaseModel):
    deleted: bool


class MemoryDeleteResponse(ResponseEnvelope):
    data: MemoryDeleteData


class MemoryAddResponse(BaseModel):
    job_id: str | None = None
    status: str
    blocked_reason: str | None = None
    retry_after_seconds: int | None = None
    budget_remaining_pct: float | None = None
    processing_eta_seconds: int | None = None
    processing_status: Literal["normal", "delayed"] = "normal"
    request_id: str
    timestamp: datetime


class MemoryJobStatusData(BaseModel):
    job_id: str
    status: str
    memories_created: int = 0
    attempts: int = 0
    created_at: datetime | None = None
    processing_started_at: datetime | None = None
    queue_name: str | None = None
    error: str | None = None
    error_summary: str | None = None
    queued_at: datetime | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None
    dead_lettered_at: datetime | None = None


class MemoryJobStatusResponse(ResponseEnvelope):
    data: MemoryJobStatusData


class MemoryRetrieveResponse(ResponseEnvelope):
    data: list[MemorySearchResult]
    cached: bool
    system_prompt_addition: str
    context_token_count: int = 0
    clarification_question: str | None = None
    quota_mode: str | None = None
    is_degraded: bool = False
    is_passthrough: bool = False


class UserProfileData(BaseModel):
    id: str
    external_id: str
    email: str
    settings: dict[str, Any]
    memory_count: int
    storage_bytes: int


class UserProfileResponse(ResponseEnvelope):
    data: UserProfileData


class UserSettingsData(BaseModel):
    settings: dict[str, Any]


class UserSettingsResponse(ResponseEnvelope):
    data: UserSettingsData


class ApiKeyData(BaseModel):
    id: str
    name: str
    permissions: list[str]
    rate_limit_per_minute: int
    created_at: datetime | None = None
    last_used_at: datetime | None = None
    is_active: bool


class ApiKeyCreateData(ApiKeyData):
    raw_key: str


class ApiKeyListResponse(ResponseEnvelope):
    data: list[ApiKeyData]
    pagination: CursorPage


class ApiKeyCreateResponse(ResponseEnvelope):
    data: ApiKeyCreateData


class ApiKeyDeleteData(BaseModel):
    deleted: bool


class ApiKeyDeleteResponse(ResponseEnvelope):
    data: ApiKeyDeleteData


class AgentData(BaseModel):
    id: str
    name: str
    description: str | None = None
    memory_scope: MemoryScope
    created_at: datetime | None = None


class AgentListResponse(ResponseEnvelope):
    data: list[AgentData]
    pagination: CursorPage


class AgentCreateResponse(ResponseEnvelope):
    data: AgentData


class UserExportData(BaseModel):
    user: UserProfileData
    memories: list[MemoryData]
    api_keys: list[ApiKeyData]
    agents: list[AgentData]


class UserExportResponse(ResponseEnvelope):
    data: UserExportData


class UserDeleteData(BaseModel):
    deleted: bool
    memories_removed: int


class UserDeleteResponse(ResponseEnvelope):
    data: UserDeleteData


class ProxyUserStatsData(BaseModel):
    external_user_id: str
    user_id: str | None = None
    memory_count: int
    last_active_at: datetime | None = None
    created_at: datetime | None = None


class ProxyUserStatsResponse(ResponseEnvelope):
    data: ProxyUserStatsData


class ProxyUserDeleteData(BaseModel):
    deleted: bool
    memories_removed: int


class ProxyUserDeleteResponse(ResponseEnvelope):
    data: ProxyUserDeleteData


class ProxyUserBlockData(BaseModel):
    blocked: bool


class ProxyUserBlockResponse(ResponseEnvelope):
    data: ProxyUserBlockData


class WebhookData(BaseModel):
    received: bool


class WebhookResponse(ResponseEnvelope):
    data: WebhookData


class HealthData(BaseModel):
    status: str
    qdrant: str
    postgres: str
    redis: str
    version: str


class HealthResponse(ResponseEnvelope):
    data: HealthData
