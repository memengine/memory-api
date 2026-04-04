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

MessageRole = Literal["user", "assistant", "system"]
QuotaMode = Literal["FULL", "PASSTHROUGH", "DEGRADED_RETRIEVE", "BLOCKED"]
CircuitStatus = Literal["HEALTHY", "DEGRADED", "CRITICAL"]
ProcessingStatus = Literal["normal", "delayed"]


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


class AddRequest(BaseModel):
    external_user_id: str = Field(min_length=1)
    agent_id: str | None = None
    messages: list[ConversationMessage] = Field(min_length=1)
    metadata: dict[str, Any] = Field(default_factory=dict)


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


class MemoryResult(BaseModel):
    id: str
    content: str
    category: MemoryCategory
    importance_score: float
    last_accessed: datetime | None = None
    relevance_score: float
    context_snippet: str


class RetrieveResult(BaseModel):
    items: list[MemoryResult] = Field(default_factory=list)
    cached: bool = False
    system_prompt_addition: str = ""
    quota_mode: QuotaMode = "FULL"
    is_passthrough: bool = False
    is_degraded: bool = False
    circuit_status: CircuitStatus = "HEALTHY"

    def __iter__(self):
        return iter(self.items)

    def __len__(self) -> int:
        return len(self.items)

    def __getitem__(self, index: int) -> MemoryResult:
        return self.items[index]


class MemoryRecord(BaseModel):
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


class RetrieveEnvelope(EnvelopeBase):
    data: list[MemoryResult]
    cached: bool
    system_prompt_addition: str
    quota_mode: QuotaMode = "FULL"


class MemoryListEnvelope(EnvelopeBase):
    data: list[MemoryRecord]
    pagination: CursorPageInfo


class ExportEnvelope(EnvelopeBase):
    data: MemoryExport


class DeleteEnvelope(EnvelopeBase):
    data: DeleteResult
