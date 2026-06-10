from __future__ import annotations

import asyncio
import json
import logging
import os
import secrets
import uuid
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
from fastapi import Query
from fastapi import Request
from jose import JWTError
from jose import jwt
from sqlalchemy import desc
from sqlalchemy import func
from sqlalchemy import select
from sqlalchemy import update
from sqlalchemy.orm import selectinload

from api.db.models import UserMemoryFlag
from api.db.models import ClarificationQueue
from api.db.models import ClarificationQueueStatus
from api.db.models import CrossUserConflict
from api.db.models import CrossUserConflictStatus
from api.db.models import GlobalAgent
from api.db.models import Memory
from api.db.models import PermissionGrant
from api.db.models import UUIProxyLink
from api.db.models import UniversalMemory
from api.db.models import UniversalMemoryVersion
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
from api.schemas.uui_schemas import ClarificationAnswerData
from api.schemas.uui_schemas import ClarificationAnswerRequest
from api.schemas.uui_schemas import ClarificationAnswerResponse
from api.schemas.uui_schemas import ClarificationItem
from api.schemas.uui_schemas import ClarificationListData
from api.schemas.uui_schemas import ClarificationListResponse
from api.schemas.uui_schemas import DomainProfileData
from api.schemas.uui_schemas import DomainProfileResponse
from api.schemas.uui_schemas import EdTechTopicSummary
from api.schemas.uui_schemas import EdTechUserProfile
from api.schemas.uui_schemas import MemoryPreviewData
from api.schemas.uui_schemas import MemoryPreviewResponse
from api.schemas.uui_schemas import PermissionGrantData
from api.schemas.uui_schemas import PermissionGrantListData
from api.schemas.uui_schemas import PermissionGrantListResponse
from api.schemas.uui_schemas import PermissionGrantResponse
from api.schemas.uui_schemas import PartialGrantUpdate
from api.schemas.uui_schemas import RevokeGrantData
from api.schemas.uui_schemas import RevokeGrantResponse
from api.schemas.uui_schemas import SendOTPRequest
from api.schemas.uui_schemas import SessionUserData
from api.schemas.uui_schemas import SessionUserResponse
from api.schemas.uui_schemas import TokenRegenerateData
from api.schemas.uui_schemas import TokenRegenerateResponse
from api.schemas.uui_schemas import UUIGrantCreateRequest
from api.schemas.uui_schemas import UUIRegisterRequest
from api.schemas.uui_schemas import UserMemoryCorrectData
from api.schemas.uui_schemas import UserMemoryCorrectRequest
from api.schemas.uui_schemas import UserMemoryCorrectResponse
from api.schemas.uui_schemas import UserMemoryDeleteData
from api.schemas.uui_schemas import UserMemoryDeleteResponse
from api.schemas.uui_schemas import UserMemoryFlagData
from api.schemas.uui_schemas import UserMemoryFlagRequest
from api.schemas.uui_schemas import UserMemoryFlagResponse
from api.schemas.uui_schemas import UserMemoryListResponse
from api.schemas.uui_schemas import UserMemoryUnflagData
from api.schemas.uui_schemas import UserMemoryUnflagResponse
from api.schemas.uui_schemas import UserMemoryView
from api.schemas.uui_schemas import UniversalMemoryHistoryResponse
from api.schemas.uui_schemas import UniversalMemoryVersionView
from api.schemas.uui_schemas import UniversalUserData
from api.schemas.uui_schemas import UniversalUserDeleteData
from api.schemas.uui_schemas import UniversalUserDeleteResponse
from api.schemas.uui_schemas import UniversalUserResponse
from api.schemas.uui_schemas import VerifyOTPRequest
from api.services.email_service import EmailService
from api.services.embedding_service import EmbeddingService
from api.services.uui_service import UUIService
from api.services.version_service import VersionService


router = APIRouter(prefix="/v1/uui", tags=["uui"])
logger = logging.getLogger(__name__)

UUI_TOKEN_HEADER = "X-MemoryOS-UUI"
SESSION_HEADER = "X-MemoryOS-Session"
SESSION_COOKIE_NAME = "memoryos_uui_session"
SESSION_TTL_SECONDS = 7 * 24 * 60 * 60
TOKEN_REGEN_TTL_SECONDS = 24 * 60 * 60
ALLOWED_CATEGORIES = {"preference", "fact", "goal", "procedure", "relationship", "expertise"}


def _mask_uui_token(token: str | None) -> str | None:
    if not token:
        return None
    token = str(token)
    if len(token) <= 12:
        return "uui_****"
    return f"{token[:8]}...{token[-4:]}"


def _stored_ago(value: datetime | None) -> str:
    if value is None:
        return "unknown"
    now = datetime.now(UTC)
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    days = max(0, int((now - value).total_seconds() // 86400))
    if days == 0:
        return "today"
    if days == 1:
        return "1 day ago"
    return f"{days} days ago"


def _days_ago(value: datetime | None) -> int | None:
    if value is None:
        return None
    now = datetime.now(UTC)
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return max(0, int((now - value).total_seconds() // 86400))


def _importance_trend(memory: UniversalMemory) -> str:
    metadata = getattr(memory, "metadata_json", None) or {}
    original = metadata.get("original_importance_score")
    try:
        original_score = float(original)
    except (TypeError, ValueError):
        original_score = float(memory.importance_score or 0.0)
    delta = float(memory.importance_score or 0.0) - original_score
    if delta > 0.3:
        return "rising"
    if delta < -0.3:
        return "decaying"
    return "stable"


def _normalize_categories(raw_categories: str | None) -> list[str]:
    if not raw_categories:
        return []
    categories = [item.strip() for item in raw_categories.split(",") if item.strip()]
    return [category for category in categories if category in ALLOWED_CATEGORIES]


def _agent_domain_schema(agent: GlobalAgent | None) -> str | None:
    if agent is None:
        return None
    tenant = getattr(agent, "owner_tenant", None)
    metadata = getattr(tenant, "metadata_json", None) or {}
    domain = metadata.get("domain_schema") or metadata.get("memory_domain")
    return str(domain) if domain else None


def _universal_memory_payload(memory: UniversalMemory, *, vector_size: int | None = None) -> dict[str, Any]:
    return {
        "memory_id": str(memory.id),
        "user_uui_id": str(memory.user_uui_id),
        "source_agent_id": str(memory.source_agent_id),
        "category": memory.category,
        "importance_score": float(memory.importance_score or 0.0),
        "is_archived": bool(memory.is_archived),
        "created_at": memory.created_at.isoformat() if memory.created_at else datetime.now(UTC).isoformat(),
        **({"vector_size": vector_size} if vector_size else {}),
    }


async def _redis_ttl(cache_service: CacheService | None, key: str) -> int:
    if cache_service is None:
        return -2
    try:
        value = await cache_service.client.ttl(key)
        return int(value or 0)
    except Exception:
        return -2


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
        agent_domain_schema=_agent_domain_schema(agent),
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
            masked_uui_token=_mask_uui_token(universal_user.uui_token),
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
            masked_uui_token=_mask_uui_token(universal_user.uui_token),
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
        consent_base = str(os.getenv("CONSENT_APP_BASE_URL") or "").rstrip("/")
        if consent_base:
            manage_url = f"{consent_base}/manage?revoke={grant.id}"
            background_tasks.add_task(
                EmailService().send_grant_notification,
                universal_user.email,
                getattr(getattr(grant, "global_agent", None), "name", "An app"),
                list(grant.categories_allowed or []),
                manage_url,
                grant.expires_at,
            )
        else:
            logger.warning("CONSENT_APP_BASE_URL is not configured; skipping grant notification email.")

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


@router.patch("/me/grants/{grant_id}", response_model=PermissionGrantResponse)
async def update_my_grant(
    request: Request,
    grant_id: str,
    payload: PartialGrantUpdate,
    session: DbSession,
    cache_service: Annotated[CacheService, Depends(get_cache_service)],
    universal_user: Annotated[UniversalUser, Depends(_current_universal_user)],
) -> PermissionGrantResponse:
    categories = list(payload.categories_allowed or [])
    invalid = [category for category in categories if category not in ALLOWED_CATEGORIES]
    if invalid or not categories:
        raise APIError(
            status_code=422,
            code="REQ_422",
            error="validation_error",
            details={"invalid_categories": invalid},
        )

    grant = (
        await session.execute(
            select(PermissionGrant)
            .options(selectinload(PermissionGrant.global_agent).selectinload(GlobalAgent.owner_tenant))
            .where(
                PermissionGrant.id == uuid.UUID(grant_id),
                PermissionGrant.user_uui_id == universal_user.id,
                PermissionGrant.is_active.is_(True),
            )
        )
    ).scalar_one_or_none()
    if grant is None:
        raise APIError(status_code=404, code="GNT_404", error="permission_grant_not_found")

    grant.categories_allowed = categories
    await session.commit()
    await UUIService(session=session, cache_service=cache_service)._invalidate_permission_cache(
        str(universal_user.id),
        str(grant.agent_id),
    )
    await session.refresh(grant)
    if getattr(grant, "global_agent", None) is None:
        grant.global_agent = await session.get(GlobalAgent, grant.agent_id)

    return PermissionGrantResponse(
        data=_grant_to_data(grant),
        request_id=get_request_id(request),
        timestamp=utc_now(),
    )


@router.get("/me/memories/preview", response_model=MemoryPreviewResponse)
async def preview_my_memories_for_agent(
    request: Request,
    session: DbSession,
    universal_user: Annotated[UniversalUser, Depends(_current_universal_user)],
    agent_id: str = Query(...),
    categories: str | None = Query(default=None),
) -> MemoryPreviewResponse:
    agent = await session.get(GlobalAgent, uuid.UUID(agent_id))
    if agent is None or not bool(agent.is_active):
        raise APIError(status_code=404, code="AGN_404", error="global_agent_not_found")

    requested = _normalize_categories(categories)
    if not requested:
        requested = [
            category
            for category in list(agent.default_categories_requested or [])
            if category in ALLOWED_CATEGORIES
        ]
    if not requested:
        requested = sorted(ALLOWED_CATEGORIES)

    memories = (
        await session.execute(
            select(UniversalMemory)
            .where(
                UniversalMemory.user_uui_id == universal_user.id,
                UniversalMemory.is_archived.is_(False),
                UniversalMemory.category.in_(requested),
            )
            .order_by(desc(UniversalMemory.importance_score), desc(UniversalMemory.created_at))
            .limit(5)
        )
    ).scalars().all()

    return MemoryPreviewResponse(
        data=[
            MemoryPreviewData(
                content_preview=(
                    memory.content[:100] + "..."
                    if len(memory.content) > 100
                    else memory.content
                ),
                category=memory.category,
                importance_score=float(memory.importance_score or 0.0),
                stored_ago=_stored_ago(memory.created_at),
            )
            for memory in memories
        ],
        request_id=get_request_id(request),
        timestamp=utc_now(),
    )


def _memory_to_view(memory: UniversalMemory, *, active_agent_ids: set[str]) -> UserMemoryView:
    metadata = getattr(memory, "metadata_json", None) or {}
    stored_days = _days_ago(memory.created_at)
    last_accessed_days = _days_ago(memory.last_accessed_at)
    return UserMemoryView(
        id=memory.id,
        content=memory.content,
        category=memory.category,
        importance_score=float(memory.importance_score or 0.0),
        importance_trend=_importance_trend(memory),
        is_hot=bool(metadata.get("is_hot", False)),
        stored_days_ago=stored_days if stored_days is not None else 0,
        last_accessed_days_ago=last_accessed_days,
        source_agent_name=getattr(getattr(memory, "source_agent", None), "name", None),
        source_agent_access_revoked=str(memory.source_agent_id) not in active_agent_ids,
        stored_at=memory.created_at,
        is_flagged=bool(getattr(memory, "is_flagged", False)),
    )


def _memory_domain(memory: UniversalMemory | Memory | None) -> str | None:
    metadata = getattr(memory, "metadata_json", None) or {}
    domain = metadata.get("domain") or metadata.get("source_domain")
    return str(domain).lower() if domain else None


def _memory_field(memory: UniversalMemory | Memory | None) -> str | None:
    metadata = getattr(memory, "metadata_json", None) or {}
    field = metadata.get("field") or metadata.get("source_field")
    return str(field) if field else None


def _readable_domain_value(value: Any, *, field: str | None = None) -> str | None:
    if value is None:
        return None
    if isinstance(value, dict):
        if field and "weak_topic" in field:
            return str(value.get("topic") or value.get("name") or value.get("concept") or value)
        if field and "exam" in field:
            pieces = [value.get("exam_name") or value.get("name"), value.get("exam_date") or value.get("date")]
            text = " ".join(str(piece) for piece in pieces if piece)
            return text or str(value)
        for key in ("value", "topic", "name", "grade_level", "exam_name", "content"):
            if value.get(key):
                return str(value[key])
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, list):
        return ", ".join(_readable_domain_value(item, field=field) or "" for item in value[:3]).strip(", ")
    return str(value)


def _domain_from_metadata(metadata: dict[str, Any]) -> str | None:
    domain = metadata.get("domain") or metadata.get("source_domain")
    return str(domain).lower() if domain else None


def _field_from_metadata(metadata: dict[str, Any]) -> str | None:
    field = metadata.get("field") or metadata.get("source_field")
    return str(field) if field else None


def _topic_from_memory(memory: UniversalMemory) -> str:
    metadata = memory.metadata_json or {}
    value = metadata.get("value") or metadata.get("topic") or metadata.get("payload")
    readable = _readable_domain_value(value, field=_field_from_metadata(metadata))
    if readable:
        return readable
    content = memory.content
    for prefix in (
        "Student is working on improving ",
        "Student is strong in ",
    ):
        if content.startswith(prefix):
            return content[len(prefix):].split(":")[0].strip(". ")
    return content[:120]


def _edtech_profile_from_memories(memories: list[UniversalMemory]) -> EdTechUserProfile:
    grade_level: str | None = None
    board: str | None = None
    exam_name: str | None = None
    exam_date: str | None = None
    marks_target: dict | None = None
    explanation_style: dict | None = None
    language_profile: dict | None = None
    weak_topics: list[EdTechTopicSummary] = []
    strong_topics: list[EdTechTopicSummary] = []
    forgetting_stages: dict[str, str] = {}
    source_agent_ids: set[str] = set()

    for memory in memories:
        metadata = memory.metadata_json or {}
        source_agent_ids.add(str(memory.source_agent_id))
        field = _field_from_metadata(metadata) or ""
        value = metadata.get("value") or metadata.get("payload")
        readable = _readable_domain_value(value, field=field)

        if "grade_level" in field:
            grade_level = readable or memory.content.removeprefix("Student is in ").strip(". ")
        elif "board_or_curriculum" in field or field.endswith("board"):
            board = readable or memory.content.removeprefix("Student follows ").strip(". ")
        elif "exam_name" in field or "exam_context" in field:
            exam_name = readable or memory.content.removeprefix("Student is preparing for ").strip(". ")
        elif "exam_date" in field:
            exam_date = readable
        elif "marks_target" in field and isinstance(value, dict):
            marks_target = value
        elif "weak_topic" in field:
            topic = _topic_from_memory(memory)
            weak_topics.append(
                EdTechTopicSummary(
                    topic=topic,
                    severity=str(metadata.get("severity")) if metadata.get("severity") else None,
                    attempts=int(metadata["attempts"]) if str(metadata.get("attempts", "")).isdigit() else None,
                )
            )
        elif "strong_topic" in field:
            strong_topics.append(
                EdTechTopicSummary(
                    topic=_topic_from_memory(memory),
                    confidence=float(metadata.get("confidence") or memory.confidence or 0.0),
                )
            )
        elif "explanation_style" in field:
            explanation_style = value if isinstance(value, dict) else {"primary": readable or memory.content}
        elif "language_profile" in field:
            language_profile = value if isinstance(value, dict) else {"primary": readable or memory.content}

        if isinstance(metadata.get("forgetting_stage"), str):
            forgetting_stages[_topic_from_memory(memory)] = str(metadata["forgetting_stage"])

    days_to_exam = None
    if exam_date:
        try:
            parsed = datetime.fromisoformat(exam_date).date()
            days_to_exam = (parsed - utc_now().date()).days
        except ValueError:
            days_to_exam = None

    return EdTechUserProfile(
        grade_level=grade_level,
        board=board,
        exam_name=exam_name,
        exam_date=exam_date,
        days_to_exam=days_to_exam,
        marks_target=marks_target,
        weak_topics=weak_topics[:10],
        strong_topics=strong_topics[:10],
        forgetting_stages=forgetting_stages,
        explanation_style=explanation_style,
        language_profile=language_profile,
        total_edtech_memories=len(memories),
        source_agent_count=len(source_agent_ids),
    )


@router.get("/me/domain-profile", response_model=DomainProfileResponse)
async def get_my_domain_profile(
    request: Request,
    session: DbSession,
    universal_user: Annotated[UniversalUser, Depends(_current_universal_user)],
) -> DomainProfileResponse:
    memories = (
        await session.execute(
            select(UniversalMemory)
            .where(
                UniversalMemory.user_uui_id == universal_user.id,
                UniversalMemory.is_archived.is_(False),
            )
            .order_by(desc(UniversalMemory.importance_score), desc(UniversalMemory.created_at))
        )
    ).scalars().all()

    by_domain: dict[str, list[UniversalMemory]] = {}
    for memory in memories:
        domain = _domain_from_metadata(memory.metadata_json or {})
        if domain:
            by_domain.setdefault(domain, []).append(memory)

    detected_domain = None
    if by_domain:
        ranked = sorted(by_domain.items(), key=lambda item: len(item[1]), reverse=True)
        if len(ranked) == 1 or len(ranked[0][1]) > len(ranked[1][1]):
            detected_domain = ranked[0][0]

    edtech_memories = by_domain.get("edtech", [])
    return DomainProfileResponse(
        data=DomainProfileData(
            detected_domain=detected_domain,
            edtech_profile=(
                _edtech_profile_from_memories(edtech_memories)
                if edtech_memories
                else None
            ),
        ),
        request_id=get_request_id(request),
        timestamp=utc_now(),
    )


@router.get("/me/memories", response_model=UserMemoryListResponse)
async def list_my_universal_memories(
    request: Request,
    session: DbSession,
    universal_user: Annotated[UniversalUser, Depends(_current_universal_user)],
    category: str | None = Query(default=None),
    categories: str | None = Query(default=None),
    cursor: str | None = Query(default=None),
    limit: int = Query(default=20, ge=1, le=50),
    sort: str = Query(default="importance"),
) -> UserMemoryListResponse:
    requested = _normalize_categories(categories)
    if category and category in ALLOWED_CATEGORIES:
        requested = [category]
    try:
        offset = max(0, int(cursor or "0"))
    except ValueError:
        offset = 0

    conditions = [
        UniversalMemory.user_uui_id == universal_user.id,
        UniversalMemory.is_archived.is_(False),
    ]
    if requested:
        conditions.append(UniversalMemory.category.in_(requested))

    active_agent_ids = {
        str(agent_id)
        for agent_id in (
            await session.execute(
                select(PermissionGrant.agent_id).where(
                    PermissionGrant.user_uui_id == universal_user.id,
                    PermissionGrant.is_active.is_(True),
                    (PermissionGrant.expires_at.is_(None) | (PermissionGrant.expires_at > func.now())),
                )
            )
        ).scalars().all()
    }

    total = int(
        (
            await session.execute(
                select(func.count()).select_from(UniversalMemory).where(*conditions)
            )
        ).scalar_one()
        or 0
    )
    if sort == "recent":
        order_by = (desc(UniversalMemory.created_at), desc(UniversalMemory.importance_score))
    elif sort == "oldest":
        order_by = (UniversalMemory.created_at.asc(), desc(UniversalMemory.importance_score))
    else:
        order_by = (desc(UniversalMemory.importance_score), desc(UniversalMemory.created_at))

    memory_result = await session.execute(
        select(UniversalMemory)
        .options(selectinload(UniversalMemory.source_agent))
        .where(*conditions)
        .order_by(*order_by)
        .offset(offset)
        .limit(limit)
    )
    memories = list(memory_result.scalars().all())
    next_offset = offset + len(memories)
    next_cursor = str(next_offset) if next_offset < total else None

    return UserMemoryListResponse(
        data=[_memory_to_view(memory, active_agent_ids=active_agent_ids) for memory in memories],
        next_cursor=next_cursor,
        total_count=total,
        request_id=get_request_id(request),
        timestamp=utc_now(),
    )


async def _get_user_universal_memory(
    session: DbSession,
    *,
    memory_id: str,
    user_uui_id: uuid.UUID,
    include_archived: bool = False,
) -> UniversalMemory:
    try:
        parsed_id = uuid.UUID(memory_id)
    except ValueError:
        raise APIError(status_code=404, code="MEM_404", error="memory_not_found") from None

    conditions = [
        UniversalMemory.id == parsed_id,
        UniversalMemory.user_uui_id == user_uui_id,
    ]
    if not include_archived:
        conditions.append(UniversalMemory.is_archived.is_(False))
    memory = (
        await session.execute(
            select(UniversalMemory)
            .options(selectinload(UniversalMemory.source_agent))
            .where(*conditions)
        )
    ).scalar_one_or_none()
    if memory is None:
        raise APIError(status_code=404, code="MEM_404", error="memory_not_found")
    return memory


@router.get("/me/memories/{memory_id}/history", response_model=UniversalMemoryHistoryResponse)
async def get_my_memory_history(
    request: Request,
    memory_id: str,
    session: DbSession,
    universal_user: Annotated[UniversalUser, Depends(_current_universal_user)],
) -> UniversalMemoryHistoryResponse:
    try:
        versions = await VersionService(session).get_universal_history(
            universal_memory_id=memory_id,
            user_uui_id=str(universal_user.id),
            db_session=session,
        )
    except (PermissionError, ValueError):
        raise APIError(status_code=404, code="MEM_404", error="memory_not_found") from None

    version_ids = [version.id for version in versions]
    agent_names: dict[uuid.UUID, str | None] = {}
    if version_ids:
        rows = (
            await session.execute(
                select(UniversalMemoryVersion.id, GlobalAgent.name)
                .outerjoin(GlobalAgent, UniversalMemoryVersion.changed_by_agent_id == GlobalAgent.id)
                .where(UniversalMemoryVersion.id.in_(version_ids))
            )
        ).all()
        agent_names = {version_id: name for version_id, name in rows}

    return UniversalMemoryHistoryResponse(
        data=[
            UniversalMemoryVersionView(
                version_number=int(version.version_number),
                content=version.content,
                change_type=version.change_type,
                change_reason=version.change_reason,
                changed_by=version.changed_by,
                agent_name=agent_names.get(version.id),
                created_at=version.created_at,
                days_ago=_days_ago(version.created_at) or 0,
            )
            for version in versions
        ],
        request_id=get_request_id(request),
        timestamp=utc_now(),
    )


@router.post("/me/memories/{memory_id}/flag", response_model=UserMemoryFlagResponse)
async def flag_my_memory(
    request: Request,
    memory_id: str,
    payload: UserMemoryFlagRequest,
    session: DbSession,
    universal_user: Annotated[UniversalUser, Depends(_current_universal_user)],
) -> UserMemoryFlagResponse:
    memory = await _get_user_universal_memory(
        session,
        memory_id=memory_id,
        user_uui_id=universal_user.id,
    )
    existing_flag = (
        await session.execute(
            select(UserMemoryFlag).where(
                UserMemoryFlag.memory_id == memory.id,
                UserMemoryFlag.user_uui_id == universal_user.id,
                UserMemoryFlag.status == "pending",
            )
        )
    ).scalar_one_or_none()
    if existing_flag is None:
        session.add(
            UserMemoryFlag(
                id=uuid.uuid4(),
                memory_id=memory.id,
                user_uui_id=universal_user.id,
                reason=payload.reason,
                correction=payload.correction,
                status="pending",
            )
        )
    else:
        existing_flag.reason = payload.reason
        existing_flag.correction = payload.correction
        existing_flag.flagged_at = datetime.now(UTC)

    memory.is_flagged = True
    await session.commit()
    return UserMemoryFlagResponse(
        data=UserMemoryFlagData(flagged=True, memory_id=memory.id),
        request_id=get_request_id(request),
        timestamp=utc_now(),
    )


@router.delete("/me/memories/{memory_id}/flag", response_model=UserMemoryUnflagResponse)
async def unflag_my_memory(
    request: Request,
    memory_id: str,
    session: DbSession,
    universal_user: Annotated[UniversalUser, Depends(_current_universal_user)],
) -> UserMemoryUnflagResponse:
    memory = await _get_user_universal_memory(
        session,
        memory_id=memory_id,
        user_uui_id=universal_user.id,
    )
    await session.execute(
        update(UserMemoryFlag)
        .where(
            UserMemoryFlag.memory_id == memory.id,
            UserMemoryFlag.user_uui_id == universal_user.id,
            UserMemoryFlag.status == "pending",
        )
        .values(status="dismissed", resolved_at=datetime.now(UTC))
    )
    memory.is_flagged = False
    await session.commit()
    return UserMemoryUnflagResponse(
        data=UserMemoryUnflagData(unflagged=True, memory_id=memory.id),
        request_id=get_request_id(request),
        timestamp=utc_now(),
    )


@router.post("/me/memories/{memory_id}/correct", response_model=UserMemoryCorrectResponse)
async def correct_my_memory(
    request: Request,
    memory_id: str,
    payload: UserMemoryCorrectRequest,
    session: DbSession,
    qdrant_service: Annotated[QdrantService, Depends(get_qdrant_service)],
    universal_user: Annotated[UniversalUser, Depends(_current_universal_user)],
) -> UserMemoryCorrectResponse:
    memory = await _get_user_universal_memory(
        session,
        memory_id=memory_id,
        user_uui_id=universal_user.id,
    )
    corrected_content = payload.corrected_content.strip()
    embedding = await EmbeddingService(async_session=session).embed(corrected_content)
    now = datetime.now(UTC)
    new_memory_id = uuid.uuid4()
    old_metadata = dict(getattr(memory, "metadata_json", None) or {})

    memory.is_archived = True
    memory.is_flagged = False
    memory.metadata_json = {
        **old_metadata,
        "archived_reason": "user_correction",
        "user_corrected_at": now.isoformat(),
        "corrected_to_memory_id": str(new_memory_id),
    }
    new_memory = UniversalMemory(
        id=new_memory_id,
        user_uui_id=universal_user.id,
        source_agent_id=memory.source_agent_id,
        content=corrected_content,
        category=memory.category,
        importance_score=float(memory.importance_score or 1.0),
        confidence=float(memory.confidence or 1.0),
        embedding_id=str(new_memory_id),
        created_at=now,
        last_accessed_at=now,
        is_archived=False,
        is_flagged=False,
        metadata_json={
            "source": "user_correction",
            "corrected_from_memory_id": str(memory.id),
            "original_importance_score": old_metadata.get(
                "original_importance_score",
                float(memory.importance_score or 1.0),
            ),
        },
    )
    session.add(new_memory)
    version_service = VersionService(session)
    await version_service.record_universal_version(
        memory,
        "user_corrected",
        f"User corrected to: {corrected_content[:100]}",
        "user",
        db_session=session,
    )
    await version_service.record_universal_version(
        new_memory,
        "created",
        "Created as user correction",
        "user",
        db_session=session,
    )
    await session.execute(
        update(UserMemoryFlag)
        .where(
            UserMemoryFlag.memory_id == memory.id,
            UserMemoryFlag.user_uui_id == universal_user.id,
            UserMemoryFlag.status == "pending",
        )
        .values(status="resolved", resolved_at=now)
    )
    await session.commit()

    try:
        await asyncio.to_thread(
            qdrant_service.delete_memory,
            str(memory.id),
            collection_name=QdrantService.UNIVERSAL_COLLECTION_NAME,
        )
        await asyncio.to_thread(
            qdrant_service.upsert_memory,
            str(new_memory_id),
            embedding.vector,
            _universal_memory_payload(new_memory, vector_size=embedding.dimensions),
            collection_name=QdrantService.UNIVERSAL_COLLECTION_NAME,
            vector_size=embedding.dimensions,
        )
    except Exception as exc:
        logger.warning(
            "universal_memory_correction_vector_sync_failed",
            extra={
                "event": "universal_memory_correction_vector_sync_failed",
                "memory_id": str(memory.id),
                "new_memory_id": str(new_memory_id),
                "error": str(exc),
            },
        )

    return UserMemoryCorrectResponse(
        data=UserMemoryCorrectData(corrected=True, new_memory_id=new_memory_id),
        request_id=get_request_id(request),
        timestamp=utc_now(),
    )


@router.delete("/me/memories/{memory_id}", response_model=UserMemoryDeleteResponse)
async def delete_my_memory(
    request: Request,
    memory_id: str,
    session: DbSession,
    qdrant_service: Annotated[QdrantService, Depends(get_qdrant_service)],
    universal_user: Annotated[UniversalUser, Depends(_current_universal_user)],
) -> UserMemoryDeleteResponse:
    memory = await _get_user_universal_memory(
        session,
        memory_id=memory_id,
        user_uui_id=universal_user.id,
    )
    await VersionService(session).record_universal_version(
        memory,
        "user_removed",
        "Removed by user",
        "user",
        db_session=session,
    )
    memory.is_archived = True
    memory.is_flagged = False
    metadata = dict(getattr(memory, "metadata_json", None) or {})
    memory.metadata_json = {
        **metadata,
        "archived_reason": "deleted_by_user",
        "user_deleted_at": datetime.now(UTC).isoformat(),
    }
    await session.execute(
        update(UserMemoryFlag)
        .where(
            UserMemoryFlag.memory_id == memory.id,
            UserMemoryFlag.user_uui_id == universal_user.id,
            UserMemoryFlag.status == "pending",
        )
        .values(status="dismissed", resolved_at=datetime.now(UTC))
    )
    universal_user.memory_count = max(0, int(universal_user.memory_count or 0) - 1)
    await session.commit()

    try:
        await asyncio.to_thread(
            qdrant_service.delete_memory,
            str(memory.id),
            collection_name=QdrantService.UNIVERSAL_COLLECTION_NAME,
        )
    except Exception as exc:
        logger.warning(
            "universal_memory_delete_vector_sync_failed",
            extra={
                "event": "universal_memory_delete_vector_sync_failed",
                "memory_id": str(memory.id),
                "error": str(exc),
            },
        )

    return UserMemoryDeleteResponse(
        data=UserMemoryDeleteData(deleted=True, memory_id=memory.id),
        request_id=get_request_id(request),
        timestamp=utc_now(),
    )


@router.get("/me/clarifications", response_model=ClarificationListResponse)
async def list_my_clarifications(
    request: Request,
    session: DbSession,
    universal_user: Annotated[UniversalUser, Depends(_current_universal_user)],
) -> ClarificationListResponse:
    proxy_ids = (
        await session.execute(
            select(UUIProxyLink.proxy_user_id).where(UUIProxyLink.user_uui_id == universal_user.id)
        )
    ).scalars().all()
    if not proxy_ids:
        return ClarificationListResponse(
            data=ClarificationListData(clarifications=[]),
            request_id=get_request_id(request),
            timestamp=utc_now(),
        )

    clarifications = (
        await session.execute(
            select(ClarificationQueue)
            .options(
                selectinload(ClarificationQueue.conflict).selectinload(CrossUserConflict.user_a_memory),
                selectinload(ClarificationQueue.conflict).selectinload(CrossUserConflict.user_b_memory),
            )
            .where(
                ClarificationQueue.proxy_user_id.in_(proxy_ids),
                ClarificationQueue.status.in_(
                    [ClarificationQueueStatus.pending, ClarificationQueueStatus.triggered]
                ),
                ClarificationQueue.expires_at > func.now(),
            )
            .order_by(ClarificationQueue.created_at.desc(), ClarificationQueue.id.desc())
        )
    ).scalars().all()

    return ClarificationListResponse(
        data=ClarificationListData(
            clarifications=[
                ClarificationItem(
                    id=item.id,
                    question_context=item.question_context,
                    created_at=item.created_at,
                    expires_at=item.expires_at,
                    status=item.status.value if hasattr(item.status, "value") else str(item.status),
                    entity_type=(
                        item.conflict.entity_type.value
                        if item.conflict is not None and hasattr(item.conflict.entity_type, "value")
                        else (str(item.conflict.entity_type) if item.conflict is not None else None)
                    ),
                    domain=(
                        _memory_domain(item.conflict.user_a_memory)
                        or _memory_domain(item.conflict.user_b_memory)
                        if item.conflict is not None
                        else None
                    ),
                    field=(
                        _memory_field(item.conflict.user_a_memory)
                        or _memory_field(item.conflict.user_b_memory)
                        if item.conflict is not None
                        else None
                    ),
                    value_a=(item.conflict.entity_value_a if item.conflict is not None else None),
                    value_b=(item.conflict.entity_value_b if item.conflict is not None else None),
                    value_a_age_days=(
                        _days_ago(item.conflict.user_a_memory.created_at)
                        if item.conflict is not None and item.conflict.user_a_memory is not None
                        else None
                    ),
                    value_b_age_days=(
                        _days_ago(item.conflict.user_b_memory.created_at)
                        if item.conflict is not None and item.conflict.user_b_memory is not None
                        else None
                    ),
                )
                for item in clarifications
            ]
        ),
        request_id=get_request_id(request),
        timestamp=utc_now(),
    )


@router.post("/me/clarifications/{clarification_id}/answer", response_model=ClarificationAnswerResponse)
async def answer_my_clarification(
    request: Request,
    clarification_id: str,
    payload: ClarificationAnswerRequest,
    session: DbSession,
    universal_user: Annotated[UniversalUser, Depends(_current_universal_user)],
) -> ClarificationAnswerResponse:
    proxy_ids = (
        await session.execute(
            select(UUIProxyLink.proxy_user_id).where(UUIProxyLink.user_uui_id == universal_user.id)
        )
    ).scalars().all()
    clarification = (
        await session.execute(
            select(ClarificationQueue)
            .options(
                selectinload(ClarificationQueue.conflict).selectinload(CrossUserConflict.user_a_memory),
                selectinload(ClarificationQueue.conflict).selectinload(CrossUserConflict.user_b_memory),
            )
            .where(
                ClarificationQueue.id == uuid.UUID(clarification_id),
                ClarificationQueue.proxy_user_id.in_(proxy_ids or [uuid.UUID(int=0)]),
            )
        )
    ).scalar_one_or_none()
    if clarification is None:
        raise APIError(status_code=404, code="CLR_404", error="clarification_not_found")

    conflict = clarification.conflict
    if conflict is not None and payload.answer in {"A", "B"}:
        losing_memory = conflict.user_b_memory if payload.answer == "A" else conflict.user_a_memory
        if losing_memory is not None:
            losing_memory.is_archived = True
        conflict.status = CrossUserConflictStatus.resolved
        conflict.resolved_at = datetime.now(UTC)
        conflict.resolved_by = "user_session"
        conflict.resolution = payload.answer
        conflict.resolution_reason = payload.free_text or "User answered clarification."
        conflict.requires_attention = False
    elif conflict is not None and payload.answer == "both":
        conflict.status = CrossUserConflictStatus.resolved
        conflict.resolved_at = datetime.now(UTC)
        conflict.resolved_by = "user_session"
        conflict.resolution = "both"
        conflict.resolution_reason = payload.free_text or "User said both versions are correct."
        conflict.requires_attention = False
    elif conflict is not None:
        conflict.status = CrossUserConflictStatus.ignored
        conflict.resolved_at = datetime.now(UTC)
        conflict.resolved_by = "user_session"
        conflict.resolution = "neither"
        conflict.resolution_reason = payload.free_text or "User said neither version is correct."
        conflict.requires_attention = False

    clarification.status = ClarificationQueueStatus.resolved
    await session.commit()
    return ClarificationAnswerResponse(
        data=ClarificationAnswerData(resolved=True, clarification_id=clarification.id),
        request_id=get_request_id(request),
        timestamp=utc_now(),
    )


@router.post("/token/regenerate", response_model=TokenRegenerateResponse)
async def regenerate_my_uui_token(
    request: Request,
    session: DbSession,
    cache_service: Annotated[CacheService, Depends(get_cache_service)],
    universal_user: Annotated[UniversalUser, Depends(_current_universal_user)],
) -> TokenRegenerateResponse:
    rate_key = f"uui_token_regen:{universal_user.id}"
    ttl = await _redis_ttl(cache_service, rate_key)
    if ttl > 0:
        raise APIError(
            status_code=429,
            code="UUI_TOKEN_429",
            error="token_regeneration_rate_limited",
            details={"next_available_seconds": ttl},
        )

    service = UUIService(session=session, cache_service=cache_service)
    old_token = universal_user.uui_token
    await service._redis_delete(service._uui_cache_key(old_token))

    for _ in range(5):
        new_token = f"uui_{secrets.token_hex(24)}"
        exists = (
            await session.execute(select(UniversalUser.id).where(UniversalUser.uui_token == new_token))
        ).scalar_one_or_none()
        if exists is None:
            break
    else:
        raise APIError(status_code=500, code="SRV_500", error="token_generation_failed")

    universal_user.uui_token = new_token
    await session.commit()
    await service._cache_uui_token(new_token, str(universal_user.id))
    if cache_service is not None:
        try:
            await cache_service.client.set(rate_key, "1", ex=TOKEN_REGEN_TTL_SECONDS)
        except Exception:
            pass

    return TokenRegenerateResponse(
        data=TokenRegenerateData(
            uui_token=new_token,
            masked_uui_token=_mask_uui_token(new_token) or "uui_****",
            regenerated_at=datetime.now(UTC),
        ),
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
