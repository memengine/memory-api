from __future__ import annotations

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


class MemoryAddRequest(BaseModel):
    external_user_id: str = Field(min_length=1)
    agent_id: str | None = None
    messages: list[ConversationMessageRequest] = Field(min_length=1)
    metadata: dict[str, Any] = Field(default_factory=dict)


class MemoryRetrieveRequest(BaseModel):
    external_user_id: str = Field(min_length=1)
    query: str = Field(min_length=1)
    limit: int = Field(default=10, ge=1, le=50)
    categories: list[MemoryCategory] = Field(default_factory=list)
    agent_id: str | None = None
    time_filter_days: int | None = Field(default=None, ge=1, le=3650)
    format: MemoryFormat = "bullets"
    context_max_tokens: int = Field(default=500, ge=50, le=4000)


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
