from __future__ import annotations

from datetime import datetime
import uuid
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

MemoryFormat = Literal["bullets", "json", "xml"]
MemoryScope = Literal["private", "shared"]
ProcessingStatus = Literal["normal", "delayed"]


class ConversationMessageRequest(BaseModel):
    role: Literal["user", "assistant", "system"] = "user"
    content: str = Field(min_length=1)

    @field_validator("content")
    @classmethod
    def strip_content(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("Message content must not be empty.")
        return stripped


class EvidenceReferenceRequest(BaseModel):
    source_type: str = Field(min_length=1, max_length=50)
    reference: str = Field(min_length=1, max_length=500)
    content_hash: str | None = Field(default=None, min_length=64, max_length=64)


class MemorySourceRequest(BaseModel):
    event_id: str = Field(min_length=1, max_length=255)
    service: str = Field(min_length=1, max_length=100, pattern=r"^[a-z0-9][a-z0-9._-]*$")
    observed_at: datetime
    scope: dict[str, Any] = Field(default_factory=dict)
    evidence: list[EvidenceReferenceRequest] = Field(default_factory=list, max_length=20)


class MemoryAddRequest(BaseModel):
    external_user_id: str = Field(min_length=1)
    agent_id: str | None = None
    messages: list[ConversationMessageRequest] = Field(min_length=1)
    metadata: dict[str, Any] = Field(default_factory=dict)
    source: MemorySourceRequest | None = None


class MemoryRetrieveRequest(BaseModel):
    external_user_id: str = Field(min_length=1)
    query: str = Field(min_length=1)
    limit: int = Field(default=10, ge=1, le=50)
    categories: list[MemoryCategory] = Field(default_factory=list)
    agent_id: str | None = None
    time_filter_days: int | None = Field(default=None, ge=1, le=3650)
    as_of: datetime | None = None
    format: MemoryFormat = "bullets"

    @field_validator("as_of")
    @classmethod
    def require_as_of_timezone(cls, value: datetime | None) -> datetime | None:
        if value is not None and value.tzinfo is None:
            raise ValueError("as_of must include a timezone")
        return value
    context_max_tokens: int = Field(default=500, ge=50, le=4000)


RetrievalFeedbackOutcome = Literal[
    "used_successfully",
    "used_partially",
    "ignored",
    "not_useful",
    "user_corrected",
    "clarification_needed",
]


class RetrievalFeedbackRequest(BaseModel):
    retrieval_id: uuid.UUID
    outcome: RetrievalFeedbackOutcome
    used_memory_ids: list[uuid.UUID] = Field(default_factory=list, max_length=50)
    correction: str | None = Field(default=None, min_length=1, max_length=4000)
    agent_confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("correction")
    @classmethod
    def strip_correction(cls, value: str | None) -> str | None:
        if value is None:
            return None
        stripped = value.strip()
        if not stripped:
            raise ValueError("Correction must not be empty when provided.")
        return stripped


class MemoryUpdateRequest(BaseModel):
    content: str | None = Field(default=None, min_length=1)
    importance_score: float | None = Field(default=None, ge=1.0, le=10.0)
    is_archived: bool | None = None


class UserSettingsUpdateRequest(BaseModel):
    settings: dict[str, Any] = Field(default_factory=dict)


class ApiKeyCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    permissions: list[str] = Field(default_factory=list)
    rate_limit_per_minute: int = Field(default=60, ge=1, le=10_000)


class AgentCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    description: str | None = None
    memory_scope: MemoryScope = "private"
