from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel
from pydantic import Field
from pydantic import field_validator

from api.schemas.responses import ResponseEnvelope


class AuthorityRules(BaseModel):
    default_priority: int = Field(default=50, ge=0, le=100)
    categories: dict[str, int] = Field(default_factory=dict)
    domain_fields: dict[str, int] = Field(default_factory=dict)

    @field_validator("categories", "domain_fields")
    @classmethod
    def validate_priorities(cls, value: dict[str, int]) -> dict[str, int]:
        for key, priority in value.items():
            if not key.strip():
                raise ValueError("Authority rule keys must not be empty.")
            if priority < 0 or priority > 100:
                raise ValueError("Authority priorities must be between 0 and 100.")
        return value


class ServiceWriterCreateRequest(BaseModel):
    service_key: str = Field(
        min_length=1, max_length=100, pattern=r"^[a-z0-9][a-z0-9._-]*$"
    )
    display_name: str = Field(min_length=1, max_length=200)
    api_key_id: str | None = None
    authority_rules: AuthorityRules = Field(default_factory=AuthorityRules)


class ServiceWriterUpdateRequest(BaseModel):
    display_name: str | None = Field(default=None, min_length=1, max_length=200)
    api_key_id: str | None = None
    authority_rules: AuthorityRules | None = None
    is_active: bool | None = None


class ServiceWriterData(BaseModel):
    id: str
    service_key: str
    display_name: str
    api_key_id: str | None = None
    authority_rules: dict[str, Any] = Field(default_factory=dict)
    is_active: bool
    created_at: datetime
    updated_at: datetime


class ServiceWriterListResponse(ResponseEnvelope):
    data: list[ServiceWriterData]


class ServiceWriterResponse(ResponseEnvelope):
    data: ServiceWriterData


class MemorySourceEventData(BaseModel):
    id: str
    external_user_id: str
    writer_id: str | None = None
    api_key_id: str | None = None
    source_service: str
    source_event_id: str
    observed_at: datetime
    received_at: datetime
    payload_hash: str
    scope: dict[str, Any] = Field(default_factory=dict)
    evidence_refs: list[dict[str, Any]] = Field(default_factory=list)
    processing_metadata: dict[str, Any] = Field(default_factory=dict)
    extraction_job_id: str | None = None


class MemorySourceEventListResponse(ResponseEnvelope):
    data: list[MemorySourceEventData]


class MemoryClaimRevisionData(BaseModel):
    id: str
    memory_id: str | None = None
    source_event_id: str | None = None
    source_writer_id: str | None = None
    source_domain: str | None = None
    source_domain_record_id: str | None = None
    source_field: str | None = None
    source_service: str | None = None
    source_event_key: str | None = None
    asserted_value: str
    status: str
    authority_priority: int
    confidence_score: float
    observed_at: datetime | None = None
    evidence_refs: list[dict[str, Any]] = Field(default_factory=list)
    resolution_reason: str | None = None
    schema_version: int = 1
    processor_version: str = "legacy"
    created_at: datetime


class MemoryClaimData(BaseModel):
    id: str
    external_user_id: str
    category: str
    claim_fingerprint: str
    subject_key: str
    predicate_key: str
    scope: dict[str, Any] = Field(default_factory=dict)
    active_value: str | None = None
    status: str
    active_memory_id: str | None = None
    winning_revision_id: str | None = None
    authority_priority: int
    confidence_score: float
    observed_at: datetime | None = None
    effective_at: datetime
    created_at: datetime
    updated_at: datetime
    revisions: list[MemoryClaimRevisionData] = Field(default_factory=list)


class MemoryClaimListResponse(ResponseEnvelope):
    data: list[MemoryClaimData]


class MemoryClaimResponse(ResponseEnvelope):
    data: MemoryClaimData
