from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel
from pydantic import Field
from pydantic import field_validator
from pydantic import model_validator


AllowedCategory = Literal["preference", "fact", "goal", "procedure", "relationship", "expertise"]
AccessType = Literal["read_only", "read_write"]
OrganisationCategory = Literal["ecommerce", "banking", "travel", "telecom", "edtech", "saas", "other"]


class RegisterRequest(BaseModel):
    email: str | None = None
    display_name: str | None = None


class RegisterResponse(BaseModel):
    user_uui_id: UUID
    uui_token: str
    email: str | None = None


class SendOTPRequest(BaseModel):
    email: str


class VerifyOTPRequest(BaseModel):
    email: str
    otp: str = Field(min_length=6, max_length=6)


class OTPLoginResponse(BaseModel):
    user_uui_id: UUID
    email: str | None = None
    display_name: str | None = None
    session_token: str | None = None


class CreateGrantRequest(BaseModel):
    agent_id: UUID
    categories_allowed: list[AllowedCategory] = Field(min_length=1)
    access_type: AccessType = "read_only"
    expires_at: datetime | None = None


class GrantResponse(BaseModel):
    id: UUID
    agent_id: UUID
    agent_name: str | None = None
    categories_allowed: list[str]
    access_type: str
    granted_at: datetime | None = None
    expires_at: datetime | None = None
    is_active: bool = True


class PermissionsResponse(BaseModel):
    user_uui_id: UUID
    grants: list[GrantResponse]


class GlobalAgentPublic(BaseModel):
    id: UUID
    name: str
    description: str | None = None
    logo_url: str | None = None
    website_url: str | None = None
    redirect_uri: str = ""
    is_verified: bool = False
    default_categories_requested: list[str]
    owner_tenant: dict[str, str | None] | None = None


class GlobalAgentCreateRequest(BaseModel):
    name: str
    description: str | None = None
    logo_url: str | None = None
    website_url: str | None = None
    default_categories_requested: list[AllowedCategory] = Field(default_factory=list)
    redirect_uri: str


class UniversalUserData(BaseModel):
    id: UUID
    uui_token: str
    email: str | None = None
    display_name: str | None = None
    created_at: datetime | None = None
    is_active: bool = True
    memory_count: int = 0
    message: str | None = None


class PermissionGrantData(BaseModel):
    id: UUID
    user_uui_id: UUID
    agent_id: UUID
    agent_name: str | None = None
    agent_logo_url: str | None = None
    agent_website_url: str | None = None
    agent_is_verified: bool = False
    agent_domain_schema: str | None = None
    categories_allowed: list[str]
    access_type: str
    granted_at: datetime | None = None
    expires_at: datetime | None = None
    is_active: bool = True
    revoked_at: datetime | None = None


class GlobalAgentData(BaseModel):
    id: UUID
    owner_tenant_id: UUID
    name: str
    description: str | None = None
    logo_url: str | None = None
    website_url: str | None = None
    default_categories_requested: list[str]
    redirect_uri: str = ""
    is_verified: bool = False
    is_public: bool = True
    created_at: datetime | None = None
    is_active: bool = True


class GlobalAgentRegistrationData(GlobalAgentData):
    raw_agent_api_key: str


class UUIRegisterRequest(RegisterRequest):
    pass


class UUIGrantCreateRequest(BaseModel):
    agent_id: UUID
    link_token: str | None = Field(default=None, min_length=16, max_length=512)
    categories: list[AllowedCategory] | None = None
    categories_allowed: list[AllowedCategory] | None = None
    access_type: AccessType = "read_write"
    duration_days: int | None = None
    expires_at: datetime | None = None

    @field_validator("duration_days")
    @classmethod
    def validate_duration_days(cls, value: int | None) -> int | None:
        if value is None:
            return None
        if value not in {30, 90, 365}:
            raise ValueError("duration_days must be one of 30, 90, or 365.")
        return value

    @model_validator(mode="after")
    def validate_category_inputs(self) -> "UUIGrantCreateRequest":
        categories = self.categories or self.categories_allowed or []
        if not categories:
            raise ValueError("At least one category must be selected.")
        if self.duration_days is not None and self.expires_at is not None:
            raise ValueError("Provide either duration_days or expires_at, not both.")
        return self

    @property
    def resolved_categories(self) -> list[AllowedCategory]:
        return list(self.categories or self.categories_allowed or [])


class UUIResponseEnvelope(BaseModel):
    request_id: str
    timestamp: datetime


class UniversalUserResponse(UUIResponseEnvelope):
    data: UniversalUserData


class PermissionGrantResponse(UUIResponseEnvelope):
    data: PermissionGrantData


class PermissionGrantListData(BaseModel):
    grants: list[PermissionGrantData]
    memory_count: int = 0
    email: str | None = None
    display_name: str | None = None
    masked_uui_token: str | None = None


class PermissionGrantListResponse(UUIResponseEnvelope):
    data: PermissionGrantListData


class RevokeGrantData(BaseModel):
    revoked: bool


class RevokeGrantResponse(UUIResponseEnvelope):
    data: RevokeGrantData


class GlobalAgentPublicResponse(UUIResponseEnvelope):
    data: GlobalAgentPublic


class GlobalAgentRegistrationResponse(UUIResponseEnvelope):
    data: GlobalAgentRegistrationData


class GlobalAgentListResponse(UUIResponseEnvelope):
    data: list[GlobalAgentData]


class UniversalUserDeleteData(BaseModel):
    deleted: bool
    memories_removed: int


class UniversalUserDeleteResponse(UUIResponseEnvelope):
    data: UniversalUserDeleteData


class OTPSendData(BaseModel):
    sent: bool
    reason: str | None = None


class OTPSendResponse(UUIResponseEnvelope):
    data: OTPSendData


class OTPVerifyData(BaseModel):
    user_uui_id: UUID
    email: str | None = None
    display_name: str | None = None
    session_token: str


class OTPVerifyResponse(UUIResponseEnvelope):
    data: OTPVerifyData


class SessionUserData(BaseModel):
    user_uui_id: UUID
    email: str | None = None
    display_name: str | None = None
    memory_count: int = 0
    grants: list[PermissionGrantData] = Field(default_factory=list)
    masked_uui_token: str | None = None


class SessionUserResponse(UUIResponseEnvelope):
    data: SessionUserData


class MemoryPreviewData(BaseModel):
    content_preview: str
    category: str
    importance_score: float
    stored_ago: str


class MemoryPreviewResponse(UUIResponseEnvelope):
    data: list[MemoryPreviewData]


class EdTechTopicSummary(BaseModel):
    topic: str
    severity: str | None = None
    attempts: int | None = None
    confidence: float | None = None


class EdTechUserProfile(BaseModel):
    grade_level: str | None = None
    board: str | None = None
    exam_name: str | None = None
    exam_date: str | None = None
    days_to_exam: int | None = None
    marks_target: dict | None = None
    weak_topics: list[EdTechTopicSummary] = Field(default_factory=list)
    strong_topics: list[EdTechTopicSummary] = Field(default_factory=list)
    forgetting_stages: dict[str, str] = Field(default_factory=dict)
    explanation_style: dict | None = None
    language_profile: dict | None = None
    total_edtech_memories: int = 0
    source_agent_count: int = 0


class DomainProfileData(BaseModel):
    detected_domain: str | None = None
    edtech_profile: EdTechUserProfile | None = None


class DomainProfileResponse(UUIResponseEnvelope):
    data: DomainProfileData


class UniversalMemoryAuditData(BaseModel):
    id: UUID
    content: str
    category: str
    importance_score: float
    stored_at: datetime | None = None
    stored_ago: str
    importance_trend: str = "stable"
    last_accessed_by_agent: str | None = None


class UserMemoryView(BaseModel):
    id: UUID
    content: str
    category: str
    importance_score: float
    importance_trend: str = "stable"
    is_hot: bool = False
    stored_days_ago: int
    last_accessed_days_ago: int | None = None
    source_agent_name: str | None = None
    source_agent_access_revoked: bool = False
    source_type: Literal["passport_agent", "org_connection", "user_correction", "system"] = "passport_agent"
    source_organisation_name: str | None = None
    stored_at: datetime | None = None
    is_flagged: bool = False
    claim_status: Literal["active", "disputed", "archived"] | None = None
    claim_revision_status: Literal["asserted", "activated", "superseded", "disputed", "archived"] | None = None
    source_access_status: Literal["active", "revoked", "expired", "not_required"] | None = None
    provenance_recorded_at: datetime | None = None
    provenance_reason: str | None = None
    claim_schema_version: int | None = None
    claim_processor_version: str | None = None


class OrgDirectoryPublic(BaseModel):
    id: UUID
    display_name: str
    logo_url: str | None = None
    website_url: str | None = None
    category: OrganisationCategory
    oauth_enabled: bool
    link_token_enabled: bool
    is_verified: bool


class OrgDirectoryListResponse(UUIResponseEnvelope):
    data: list[OrgDirectoryPublic]


class VerifiedConnectionData(BaseModel):
    id: UUID
    organisation_id: UUID
    organisation_name: str
    organisation_logo_url: str | None = None
    category: OrganisationCategory
    organisation_is_verified: bool
    connection_method: Literal["oauth", "oidc", "link_token"]
    verified_at: datetime
    last_verified_at: datetime
    is_active: bool
    memory_count: int = 0


class VerifiedConnectionListResponse(UUIResponseEnvelope):
    data: list[VerifiedConnectionData]


class OAuthInitiateRequest(BaseModel):
    org_directory_id: UUID


class OAuthInitiateData(BaseModel):
    authorization_url: str


class OAuthInitiateResponse(UUIResponseEnvelope):
    data: OAuthInitiateData


class DisconnectConnectionData(BaseModel):
    disconnected: bool


class DisconnectConnectionResponse(UUIResponseEnvelope):
    data: DisconnectConnectionData


class UserMemoryListResponse(UUIResponseEnvelope):
    data: list[UserMemoryView]
    next_cursor: str | None = None
    total_count: int = 0


class UserMemoryFlagRequest(BaseModel):
    reason: Literal["incorrect", "outdated", "never_said_this"]
    correction: str | None = None


class UserMemoryFlagData(BaseModel):
    flagged: bool
    memory_id: UUID


class UserMemoryFlagResponse(UUIResponseEnvelope):
    data: UserMemoryFlagData


class UserMemoryUnflagData(BaseModel):
    unflagged: bool
    memory_id: UUID


class UserMemoryUnflagResponse(UUIResponseEnvelope):
    data: UserMemoryUnflagData


class UserMemoryCorrectRequest(BaseModel):
    corrected_content: str = Field(min_length=10, max_length=1000)


class UserMemoryCorrectData(BaseModel):
    corrected: bool
    new_memory_id: UUID


class UserMemoryCorrectResponse(UUIResponseEnvelope):
    data: UserMemoryCorrectData


class UserMemoryDeleteData(BaseModel):
    deleted: bool
    memory_id: UUID


class UserMemoryDeleteResponse(UUIResponseEnvelope):
    data: UserMemoryDeleteData


class UniversalMemoryVersionView(BaseModel):
    version_number: int
    content: str
    change_type: str
    change_reason: str | None = None
    changed_by: str
    agent_name: str | None = None
    created_at: datetime
    days_ago: int


class UniversalMemoryHistoryResponse(UUIResponseEnvelope):
    data: list[UniversalMemoryVersionView]


class ClarificationItem(BaseModel):
    id: UUID
    question_context: str
    created_at: datetime | None = None
    expires_at: datetime | None = None
    status: str
    entity_type: str | None = None
    domain: str | None = None
    field: str | None = None
    value_a: str | None = None
    value_b: str | None = None
    value_a_age_days: int | None = None
    value_b_age_days: int | None = None


class ClarificationListData(BaseModel):
    clarifications: list[ClarificationItem]


class ClarificationListResponse(UUIResponseEnvelope):
    data: ClarificationListData


class ClarificationAnswerRequest(BaseModel):
    answer: Literal["A", "B", "both", "neither"]
    free_text: str | None = None


class ClarificationAnswerData(BaseModel):
    resolved: bool
    clarification_id: UUID


class ClarificationAnswerResponse(UUIResponseEnvelope):
    data: ClarificationAnswerData


class PartialGrantUpdate(BaseModel):
    categories_allowed: list[AllowedCategory] = Field(min_length=1)


class TokenRegenerateData(BaseModel):
    uui_token: str
    masked_uui_token: str
    regenerated_at: datetime


class TokenRegenerateResponse(UUIResponseEnvelope):
    data: TokenRegenerateData
