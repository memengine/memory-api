from __future__ import annotations

import json
import logging
import os
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from typing import Annotated
from typing import Any

from fastapi import APIRouter
from fastapi import BackgroundTasks
from fastapi import Cookie
from fastapi import Depends
from fastapi import Header
from fastapi import Request
from jose import JWTError
from jose import jwt

from api.db.models import PermissionGrant
from api.db.models import UniversalUser
from api.dependencies import DbSession
from api.dependencies import get_cache_service
from api.dependencies import get_qdrant_service
from api.db.cache import CacheService
from api.db.vector_store import QdrantService
from api.errors import APIError
from api.routers.common import get_request_id
from api.routers.common import utc_now
from api.schemas.uui_schemas import OTPLoginResponse
from api.schemas.uui_schemas import OTPSendData
from api.schemas.uui_schemas import OTPSendResponse
from api.schemas.uui_schemas import OTPVerifyData
from api.schemas.uui_schemas import OTPVerifyResponse
from api.schemas.uui_schemas import PermissionGrantData
from api.schemas.uui_schemas import PermissionGrantListData
from api.schemas.uui_schemas import PermissionGrantListResponse
from api.schemas.uui_schemas import PermissionGrantResponse
from api.schemas.uui_schemas import RevokeGrantData
from api.schemas.uui_schemas import RevokeGrantResponse
from api.schemas.uui_schemas import SendOTPRequest
from api.schemas.uui_schemas import SessionUserData
from api.schemas.uui_schemas import SessionUserResponse
from api.schemas.uui_schemas import UUIGrantCreateRequest
from api.schemas.uui_schemas import UUIRegisterRequest
from api.schemas.uui_schemas import UniversalUserData
from api.schemas.uui_schemas import UniversalUserDeleteData
from api.schemas.uui_schemas import UniversalUserDeleteResponse
from api.schemas.uui_schemas import UniversalUserResponse
from api.schemas.uui_schemas import VerifyOTPRequest
from api.services.email_service import EmailService
from api.services.uui_service import UUIService


router = APIRouter(prefix="/v1/uui", tags=["uui"])
logger = logging.getLogger(__name__)

UUI_TOKEN_HEADER = "X-MemoryOS-UUI"
SESSION_HEADER = "X-MemoryOS-Session"
SESSION_COOKIE_NAME = "memoryos_uui_session"
SESSION_TTL_SECONDS = 7 * 24 * 60 * 60


def _grant_to_data(grant: PermissionGrant) -> PermissionGrantData:
    agent = getattr(grant, "global_agent", None)
    return PermissionGrantData(
        id=grant.id,
        user_uui_id=grant.user_uui_id,
        agent_id=grant.agent_id,
        agent_name=getattr(agent, "name", None),
        agent_logo_url=getattr(agent, "logo_url", None),
        agent_website_url=getattr(agent, "website_url", None),
        agent_is_verified=bool(getattr(agent, "is_verified", False)),
        categories_allowed=list(grant.categories_allowed or []),
        access_type=grant.access_type,
        granted_at=grant.granted_at,
        expires_at=grant.expires_at,
        is_active=bool(grant.is_active),
        revoked_at=grant.revoked_at,
    )


def _user_to_data(user: UniversalUser, *, message: str | None = None) -> UniversalUserData:
    return UniversalUserData(
        id=user.id,
        uui_token=user.uui_token,
        email=user.email,
        display_name=user.display_name,
        created_at=user.created_at,
        is_active=bool(user.is_active),
        memory_count=int(user.memory_count or 0),
        message=message,
    )


def _session_secret() -> str:
    secret = str(os.getenv("UUI_SESSION_SECRET") or os.getenv("SECRET_KEY") or "").strip()
    if not secret:
        raise APIError(status_code=500, code="SRV_500", error="session_secret_not_configured")
    return secret


def _session_token_for_user(user: UniversalUser) -> str:
    now = datetime.now(UTC)
    payload = {
        "sub": str(user.id),
        "user_uui_id": str(user.id),
        "email": user.email,
        "exp": int((now + timedelta(seconds=SESSION_TTL_SECONDS)).timestamp()),
        "iat": int(now.timestamp()),
    }
    return jwt.encode(payload, _session_secret(), algorithm="HS256")


def _decode_session_token(session_token: str) -> dict[str, Any] | None:
    token = str(session_token or "").strip()
    if not token:
        return None
    try:
        return jwt.decode(token, _session_secret(), algorithms=["HS256"])
    except JWTError:
        return None


async def _current_universal_user(
    request: Request,
    session: DbSession,
    cache_service: Annotated[CacheService, Depends(get_cache_service)],
    x_memoryos_session: Annotated[str | None, Header(alias=SESSION_HEADER)] = None,
    x_memoryos_uui: Annotated[str | None, Header(alias=UUI_TOKEN_HEADER)] = None,
    session_cookie: Annotated[str | None, Cookie(alias=SESSION_COOKIE_NAME)] = None,
) -> UniversalUser:
    uui_service = UUIService(session=session, cache_service=cache_service)

    session_token = str(x_memoryos_session or session_cookie or "").strip()
    session_claims = _decode_session_token(session_token) if session_token else None
    if session_claims is not None:
        user_uui_id = str(session_claims.get("user_uui_id") or session_claims.get("sub") or "").strip()
        if user_uui_id:
            user = await session.get(UniversalUser, user_uui_id)
            if user is not None and bool(user.is_active):
                return user

    token = str(x_memoryos_uui or "").strip()
    if token:
        resolve_by_token = getattr(uui_service, "resolve_by_token", None)
        user = None
        if callable(resolve_by_token):
            user = await resolve_by_token(token)
        if user is None:
            user = await uui_service.resolve(token)
        if user is not None:
            return user

    raise APIError(status_code=401, code="UUI_401", error="invalid_uui_session")


@router.post("/register", response_model=UniversalUserResponse)
async def register_uui(
    request: Request,
    payload: UUIRegisterRequest,
    session: DbSession,
    cache_service: Annotated[CacheService, Depends(get_cache_service)],
) -> UniversalUserResponse:
    uui_service = UUIService(session=session, cache_service=cache_service)
    user = await uui_service.register(
        email=payload.email,
        display_name=payload.display_name,
    )
    return UniversalUserResponse(
        data=_user_to_data(
            user,
            message="Check your email for login code" if payload.email else None,
        ),
        request_id=get_request_id(request),
        timestamp=utc_now(),
    )


@router.post("/otp/send", response_model=OTPSendResponse)
async def send_uui_otp(
    request: Request,
    payload: SendOTPRequest,
    session: DbSession,
    cache_service: Annotated[CacheService, Depends(get_cache_service)],
) -> OTPSendResponse:
    uui_service = UUIService(session=session, cache_service=cache_service)
    if await uui_service.is_otp_rate_limited(payload.email):
        return OTPSendResponse(
            data=OTPSendData(sent=False, reason="rate_limited"),
            request_id=get_request_id(request),
            timestamp=utc_now(),
        )

    sent = await uui_service.send_otp(payload.email)
    return OTPSendResponse(
        data=OTPSendData(sent=sent, reason=None),
        request_id=get_request_id(request),
        timestamp=utc_now(),
    )


@router.post("/otp/verify", response_model=OTPVerifyResponse)
async def verify_uui_otp(
    request: Request,
    payload: VerifyOTPRequest,
    session: DbSession,
    cache_service: Annotated[CacheService, Depends(get_cache_service)],
) -> OTPVerifyResponse:
    user = await UUIService(session=session, cache_service=cache_service).verify_otp(
        payload.email,
        payload.otp,
    )
    if user is None:
        raise APIError(status_code=401, code="UUI_OTP_401", error="invalid_otp")

    return OTPVerifyResponse(
        data=OTPVerifyData(
            user_uui_id=user.id,
            email=user.email,
            display_name=user.display_name,
            session_token=_session_token_for_user(user),
        ),
        request_id=get_request_id(request),
        timestamp=utc_now(),
    )


@router.get("/me", response_model=SessionUserResponse)
async def get_current_uui_user(
    request: Request,
    session: DbSession,
    cache_service: Annotated[CacheService, Depends(get_cache_service)],
    universal_user: Annotated[UniversalUser, Depends(_current_universal_user)],
) -> SessionUserResponse:
    grants = await UUIService(session=session, cache_service=cache_service).get_grants(str(universal_user.id))
    return SessionUserResponse(
        data=SessionUserData(
            user_uui_id=universal_user.id,
            email=universal_user.email,
            display_name=universal_user.display_name,
            memory_count=int(universal_user.memory_count or 0),
            grants=[_grant_to_data(grant) for grant in grants],
        ),
        request_id=get_request_id(request),
        timestamp=utc_now(),
    )


@router.get("/me/grants", response_model=PermissionGrantListResponse)
async def list_my_grants(
    request: Request,
    session: DbSession,
    cache_service: Annotated[CacheService, Depends(get_cache_service)],
    universal_user: Annotated[UniversalUser, Depends(_current_universal_user)],
) -> PermissionGrantListResponse:
    uui_service = UUIService(session=session, cache_service=cache_service)
    grants = await uui_service.get_grants(str(universal_user.id))
    return PermissionGrantListResponse(
        data=PermissionGrantListData(
            grants=[_grant_to_data(grant) for grant in grants],
            memory_count=int(universal_user.memory_count or 0),
            email=universal_user.email,
            display_name=universal_user.display_name,
        ),
        request_id=get_request_id(request),
        timestamp=utc_now(),
    )


@router.post("/me/grants", response_model=PermissionGrantResponse)
async def create_my_grant(
    request: Request,
    payload: UUIGrantCreateRequest,
    background_tasks: BackgroundTasks,
    session: DbSession,
    cache_service: Annotated[CacheService, Depends(get_cache_service)],
    universal_user: Annotated[UniversalUser, Depends(_current_universal_user)],
) -> PermissionGrantResponse:
    expires_at = payload.expires_at
    if expires_at is None and payload.duration_days is not None:
        expires_at = datetime.now(UTC) + timedelta(days=payload.duration_days)

    grant = await UUIService(session=session, cache_service=cache_service).create_grant(
        user_uui_id=str(universal_user.id),
        agent_id=str(payload.agent_id),
        categories=list(payload.resolved_categories),
        access_type=payload.access_type,
        expires_at=expires_at,
    )

    if universal_user.email:
        consent_base = str(os.getenv("CONSENT_APP_BASE_URL") or "https://consent.memoryos.io").rstrip("/")
        manage_url = f"{consent_base}/manage?revoke={grant.id}"
        background_tasks.add_task(
            EmailService().send_grant_notification,
            universal_user.email,
            getattr(getattr(grant, "global_agent", None), "name", "An app"),
            list(grant.categories_allowed or []),
            manage_url,
            grant.expires_at,
        )

    logger.info(
        json.dumps(
            {
                "event": "grant_created",
                "user_uui_id": str(universal_user.id),
                "agent_id": str(grant.agent_id),
                "categories": list(grant.categories_allowed or []),
            }
        )
    )

    return PermissionGrantResponse(
        data=_grant_to_data(grant),
        request_id=get_request_id(request),
        timestamp=utc_now(),
    )


@router.delete("/me/grants/{grant_id}", response_model=RevokeGrantResponse)
async def revoke_my_grant(
    request: Request,
    grant_id: str,
    session: DbSession,
    cache_service: Annotated[CacheService, Depends(get_cache_service)],
    universal_user: Annotated[UniversalUser, Depends(_current_universal_user)],
) -> RevokeGrantResponse:
    revoked = await UUIService(session=session, cache_service=cache_service).revoke_grant(
        user_uui_id=str(universal_user.id),
        grant_id=grant_id,
    )
    if not revoked:
        raise APIError(status_code=404, code="GNT_404", error="permission_grant_not_found")

    return RevokeGrantResponse(
        data=RevokeGrantData(revoked=True),
        request_id=get_request_id(request),
        timestamp=utc_now(),
    )


@router.delete("/me", response_model=UniversalUserDeleteResponse)
async def delete_my_uui_data(
    request: Request,
    session: DbSession,
    cache_service: Annotated[CacheService, Depends(get_cache_service)],
    qdrant_service: Annotated[QdrantService, Depends(get_qdrant_service)],
    universal_user: Annotated[UniversalUser, Depends(_current_universal_user)],
) -> UniversalUserDeleteResponse:
    deleted, memories_removed = await UUIService(
        session=session,
        cache_service=cache_service,
        qdrant_service=qdrant_service,
    ).delete_user_data(uui_token=universal_user.uui_token)
    if not deleted:
        raise APIError(status_code=404, code="UUI_404", error="universal_user_not_found")

    return UniversalUserDeleteResponse(
        data=UniversalUserDeleteData(
            deleted=True,
            memories_removed=memories_removed,
        ),
        request_id=get_request_id(request),
        timestamp=utc_now(),
    )
