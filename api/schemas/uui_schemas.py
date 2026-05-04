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
    is_verified: bool = False
    default_categories_requested: list[str]


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


class SessionUserResponse(UUIResponseEnvelope):
    data: SessionUserData
