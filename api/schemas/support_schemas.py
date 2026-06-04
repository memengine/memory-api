from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any
from typing import Literal

from pydantic import BaseModel
from pydantic import Field

from api.schemas.responses import CursorPage
from api.schemas.responses import ResponseEnvelope


SupportTypeValue = Literal[
    "saas",
    "ecommerce",
    "banking_fintech",
    "travel",
    "telecom",
    "edtech_support",
    "general_info",
]
SupportTypeModeValue = Literal["single", "multi", "auto"]


@dataclass(slots=True)
class SupportExtractionResult:
    fields_updated: list[str]
    nothing_to_extract: bool
    tokens_used: int
    provider_used: str
    support_type: str
    support_type_source: str = "detected"
    support_type_confidence: float | None = None
    redactions_count: int = 0


@dataclass(slots=True)
class SupportRetrieveResult:
    system_prompt_addition: str
    context_token_count: int


class SupportCustomerSummary(BaseModel):
    external_user_id: str
    customer_tier: str | None = None
    support_type: str | None = None
    sentiment_pattern: str | None = None
    open_issues_count: int = 0
    total_issues_lifetime: int = 0
    last_contact: datetime | None = None


class TenantSupportCustomersResponse(ResponseEnvelope):
    data: list[SupportCustomerSummary]
    pagination: CursorPage


class TenantSupportStatsData(BaseModel):
    total_customers_with_memory: int = 0
    open_issues_count: int = 0
    high_escalation_risk_count: int = 0
    sentiment_breakdown: dict[str, int] = Field(default_factory=dict)
    support_type_distribution: dict[str, int] = Field(default_factory=dict)
    avg_issues_per_customer: float = 0.0


class TenantSupportStatsResponse(ResponseEnvelope):
    data: TenantSupportStatsData


class TenantSupportTypePatchRequest(BaseModel):
    support_type: SupportTypeValue | None = None
    support_type_mode: SupportTypeModeValue = "single"
    support_types_allowed: list[SupportTypeValue] = Field(default_factory=list)


class TenantSupportTypeData(BaseModel):
    support_type_configured: SupportTypeValue | None = None
    support_type_mode: SupportTypeModeValue = "single"
    support_types_allowed: list[SupportTypeValue] = Field(default_factory=list)


class TenantSupportTypeResponse(ResponseEnvelope):
    data: TenantSupportTypeData


class SupportMemoryView(BaseModel):
    id: str
    proxy_user_id: str
    tenant_id: str
    support_type: str | None = None
    support_type_source: str = "detected"
    customer_identity: dict[str, Any] = Field(default_factory=dict)
    communication_preference: dict[str, Any] = Field(default_factory=dict)
    language_profile: dict[str, Any] = Field(default_factory=dict)
    current_open_issue: dict[str, Any] | None = None
    issue_history: list[dict[str, Any]] = Field(default_factory=list)
    resolution_preference: dict[str, Any] = Field(default_factory=dict)
    sentiment_pattern: str | None = None
    risk_signals: list[dict[str, Any]] = Field(default_factory=list)
    support_context: dict[str, Any] = Field(default_factory=dict)
    schema_version: int = 1
    last_extraction_at: datetime | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


class SupportMemoryResponse(ResponseEnvelope):
    data: SupportMemoryView | None


__all__ = [
    "SupportExtractionResult",
    "SupportMemoryResponse",
    "SupportMemoryView",
    "SupportRetrieveResult",
    "SupportTypeModeValue",
    "SupportTypeValue",
    "TenantSupportCustomersResponse",
    "TenantSupportStatsData",
    "TenantSupportStatsResponse",
    "TenantSupportTypeData",
    "TenantSupportTypePatchRequest",
    "TenantSupportTypeResponse",
]
