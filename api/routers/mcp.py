from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Query, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from api.db.cache import CacheService
from api.db.models import (
    ClarificationQueue,
    ClarificationQueueStatus,
    CrossUserConflict,
    CrossUserConflictStatus,
)
from api.dependencies import (
    DbSession,
    get_cache_service,
    get_context_builder,
    get_memory_service,
    get_proxy_user_service,
    get_quality_gate_service,
    get_retriever_service,
)
from api.errors import APIError
from api.routers.common import get_request_id, utc_now
from api.routers.memories import (
    _memory_to_data,
    add_memories,
    list_memories,
    retrieve_memories,
)
from api.schemas.requests import (
    ConversationMessageRequest,
    MemoryAddRequest,
    MemoryRetrieveRequest,
)
from api.schemas.responses import (
    MemoryAddResponse,
    MemoryDeleteData,
    MemoryDeleteResponse,
    MemoryGetResponse,
    MemoryListResponse,
    MemoryMutationResponse,
    MemoryRetrieveResponse,
)
from api.services.conflict_resolution_service import apply_conflict_selection
from api.services.context_builder import ContextBuilder
from api.services.global_agent_service import GlobalAgentService
from api.services.mcp_universal_capability_service import issue_universal_mcp_capability
from api.services.memory_service import MemoryService
from api.services.proxy_user_service import ProxyUserService
from api.services.quality_gate import QualityGateService
from api.services.retriever import RetrieverService
from api.services.uui_service import UUIService

router = APIRouter(prefix="/v1/mcp", tags=["mcp"])


class TenantMCPRememberRequest(BaseModel):
    """Conversation content to remember for the authenticated MCP caller."""

    messages: list[ConversationMessageRequest] = Field(min_length=1)
    metadata: dict = Field(default_factory=dict)
    agent_id: str | None = None
    conversation_id: str | None = Field(default=None, min_length=1, max_length=255)


class TenantMCPContextRequest(BaseModel):
    """A contextual query for the authenticated MCP caller."""

    query: str = Field(min_length=1)
    limit: int = Field(default=10, ge=1, le=50)
    categories: list[str] = Field(default_factory=list)
    format: str = "bullets"
    context_max_tokens: int = Field(default=500, ge=50, le=4000)


class TenantMCPSessionContextRequest(BaseModel):
    """Compact bootstrap context for an authenticated MCP chat session."""

    context_max_tokens: int = Field(default=180, ge=50, le=400)


class TenantMCPCorrectMemoryRequest(BaseModel):
    content: str = Field(min_length=1, max_length=4000)


class TenantMCPClarificationAnswerRequest(BaseModel):
    answer: str = Field(pattern="^(A|B|both|neither)$")
    reason: str | None = Field(default=None, max_length=1000)


class TenantMCPClarificationItem(BaseModel):
    id: UUID
    question_context: str
    value_a: str
    value_b: str
    entity_type: str
    created_at: datetime | None = None
    expires_at: datetime | None = None
    status: str


class TenantMCPClarificationListData(BaseModel):
    clarifications: list[TenantMCPClarificationItem]


class TenantMCPClarificationListResponse(BaseModel):
    data: TenantMCPClarificationListData
    request_id: str
    timestamp: datetime


class TenantMCPClarificationAnswerData(BaseModel):
    resolved: bool
    clarification_id: UUID


class TenantMCPClarificationAnswerResponse(BaseModel):
    data: TenantMCPClarificationAnswerData
    request_id: str
    timestamp: datetime


_SESSION_CONTEXT_QUERY = (
    "Stable user preferences, long-lived working style, active goals, important decisions, "
    "and unresolved clarifications relevant across an assistant chat session."
)
_SESSION_CONTEXT_LIMIT = 6


def _public_tenant_mcp_external_user_id(request: Request) -> str:
    """Return a stable self-only profile identity for a verified public MCP caller.

    The identity is derived server-side from the authenticated Clerk subject and
    tenant.  Public MCP callers must never supply an external user ID, which
    prevents one organisation member from selecting another member's profile.
    """
    if request.headers.get("x-memoryos-mcp-client", "").strip().lower() != "public-v1":
        raise APIError(status_code=403, code="MCP_403", error="public_mcp_marker_required")
    tenant_id = str(getattr(request.state, "tenant_id", "") or "").strip()
    clerk_subject = str(getattr(request.state, "user_id", "") or "").strip()
    if not tenant_id or not clerk_subject:
        raise APIError(status_code=401, code="AUTH_001", error="unauthorized")
    digest = hashlib.sha256(f"memoryos-mcp-profile:v1:{tenant_id}:{clerk_subject}".encode()).hexdigest()
    return f"mcp:{digest}"


def _is_self_scoped_public_clarification(
    clarification: ClarificationQueue,
    *,
    proxy_user_id: object,
) -> bool:
    """Allow public resolution only for a contradiction inside one profile.

    ``user_session`` is a routing hint, not sufficient authorization: an
    incorrectly routed cross-profile conflict must never disclose its values
    through the public MCP.  Both source memories must belong to the resolved
    public profile before a question or its A/B values are returned.
    """
    conflict = clarification.conflict
    if (
        conflict is None
        or conflict.resolution_path != "user_session"
        or conflict.user_a_memory is None
        or conflict.user_b_memory is None
    ):
        return False
    expected_proxy_id = str(proxy_user_id)
    return (
        str(clarification.proxy_user_id) == expected_proxy_id
        and str(conflict.user_a_memory.proxy_user_id) == expected_proxy_id
        and str(conflict.user_b_memory.proxy_user_id) == expected_proxy_id
    )


async def _public_tenant_mcp_proxy_user(
    *,
    request: Request,
    proxy_user_service: ProxyUserService,
):
    return await proxy_user_service.resolve(
        tenant_id=str(request.state.tenant_id),
        external_user_id=_public_tenant_mcp_external_user_id(request),
    )


@router.post("/tenant/remember", response_model=MemoryAddResponse)
async def remember_for_public_tenant_mcp(
    request: Request,
    response: Response,
    payload: TenantMCPRememberRequest,
    memory_service: Annotated[MemoryService, Depends(get_memory_service)],
    proxy_user_service: Annotated[ProxyUserService, Depends(get_proxy_user_service)],
    quality_gate_service: Annotated[QualityGateService, Depends(get_quality_gate_service)],
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> MemoryAddResponse:
    """Queue remembering for only the authenticated public MCP caller."""
    external_user_id = _public_tenant_mcp_external_user_id(request)
    return await add_memories(
        request=request,
        response=response,
        payload=MemoryAddRequest(
            external_user_id=external_user_id,
            messages=payload.messages,
            metadata={**payload.metadata, "source": "public_tenant_mcp"},
            agent_id=payload.agent_id,
            evidence_mode="client_assertion",
            conversation_id=payload.conversation_id,
        ),
        memory_service=memory_service,
        proxy_user_service=proxy_user_service,
        quality_gate_service=quality_gate_service,
        tenant_id=str(request.state.tenant_id),
        idempotency_key=idempotency_key,
    )


@router.post("/tenant/context", response_model=MemoryRetrieveResponse)
async def context_for_public_tenant_mcp(
    request: Request,
    payload: TenantMCPContextRequest,
    retriever_service: Annotated[RetrieverService, Depends(get_retriever_service)],
    proxy_user_service: Annotated[ProxyUserService, Depends(get_proxy_user_service)],
    context_builder: Annotated[ContextBuilder, Depends(get_context_builder)],
    cache_service: Annotated[CacheService, Depends(get_cache_service)],
    session: DbSession,
) -> MemoryRetrieveResponse:
    """Retrieve prompt-ready context for only the authenticated MCP caller."""
    external_user_id = _public_tenant_mcp_external_user_id(request)
    return await retrieve_memories(
        request=request,
        payload=MemoryRetrieveRequest(
            external_user_id=external_user_id,
            query=payload.query,
            limit=payload.limit,
            categories=payload.categories,
            format=payload.format,
            context_max_tokens=payload.context_max_tokens,
        ),
        retriever_service=retriever_service,
        proxy_user_service=proxy_user_service,
        context_builder=context_builder,
        cache_service=cache_service,
        session=session,
        tenant_id=str(request.state.tenant_id),
    )


@router.post("/tenant/session-context", response_model=MemoryRetrieveResponse)
async def session_context_for_public_tenant_mcp(
    request: Request,
    payload: TenantMCPSessionContextRequest,
    retriever_service: Annotated[RetrieverService, Depends(get_retriever_service)],
    proxy_user_service: Annotated[ProxyUserService, Depends(get_proxy_user_service)],
    context_builder: Annotated[ContextBuilder, Depends(get_context_builder)],
    cache_service: Annotated[CacheService, Depends(get_cache_service)],
    session: DbSession,
) -> MemoryRetrieveResponse:
    """Build a compact, self-scoped memory capsule at the start of a chat.

    The caller supplies neither an identity nor a query.  A fixed bootstrap
    query avoids leaking caller-selected profile scope while keeping ordinary
    conversation turns free of retrieval round trips.
    """
    external_user_id = _public_tenant_mcp_external_user_id(request)
    return await retrieve_memories(
        request=request,
        payload=MemoryRetrieveRequest(
            external_user_id=external_user_id,
            query=_SESSION_CONTEXT_QUERY,
            limit=_SESSION_CONTEXT_LIMIT,
            format="bullets",
            context_max_tokens=payload.context_max_tokens,
        ),
        retriever_service=retriever_service,
        proxy_user_service=proxy_user_service,
        context_builder=context_builder,
        cache_service=cache_service,
        session=session,
        tenant_id=str(request.state.tenant_id),
    )


@router.get("/tenant/memories", response_model=MemoryListResponse)
async def memories_for_public_tenant_mcp(
    request: Request,
    memory_service: Annotated[MemoryService, Depends(get_memory_service)],
    cursor: Annotated[str | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=50)] = 10,
    categories: Annotated[list[str] | None, Query()] = None,
) -> MemoryListResponse:
    """List only memories owned by the authenticated public MCP caller."""
    return await list_memories(
        request=request,
        memory_service=memory_service,
        cursor=cursor,
        limit=limit,
        categories=categories or [],
        agent_id=None,
        external_user_id=_public_tenant_mcp_external_user_id(request),
    )


@router.get("/tenant/memories/{memory_id}/why", response_model=MemoryGetResponse)
async def explain_memory_for_public_tenant_mcp(
    request: Request,
    memory_id: str,
    memory_service: Annotated[MemoryService, Depends(get_memory_service)],
) -> MemoryGetResponse:
    """Return self-owned memory provenance for an in-chat explanation."""
    external_user_id = _public_tenant_mcp_external_user_id(request)
    memory = await memory_service.get_memory(
        authenticated_user_id=None,
        memory_id=memory_id,
        tenant_id=str(request.state.tenant_id),
        external_user_id=external_user_id,
    )
    return MemoryGetResponse(data=_memory_to_data(memory), request_id=get_request_id(request), timestamp=utc_now())


@router.post("/tenant/memories/{memory_id}/correct", response_model=MemoryMutationResponse)
async def correct_memory_for_public_tenant_mcp(
    request: Request,
    memory_id: str,
    payload: TenantMCPCorrectMemoryRequest,
    memory_service: Annotated[MemoryService, Depends(get_memory_service)],
) -> MemoryMutationResponse:
    """Correct only a memory owned by the authenticated public MCP caller."""
    external_user_id = _public_tenant_mcp_external_user_id(request)
    memory = await memory_service.update_memory(
        authenticated_user_id=None,
        memory_id=memory_id,
        content=payload.content,
        importance_score=None,
        is_archived=None,
        tenant_id=str(request.state.tenant_id),
        external_user_id=external_user_id,
    )
    return MemoryMutationResponse(data=_memory_to_data(memory), request_id=get_request_id(request), timestamp=utc_now())


@router.delete("/tenant/memories/{memory_id}", response_model=MemoryDeleteResponse)
async def forget_memory_for_public_tenant_mcp(
    request: Request,
    memory_id: str,
    memory_service: Annotated[MemoryService, Depends(get_memory_service)],
) -> MemoryDeleteResponse:
    """Recoverably archive only a memory owned by the authenticated MCP caller."""
    deleted = await memory_service.delete_memory(
        authenticated_user_id=None,
        memory_id=memory_id,
        hard_delete=False,
        tenant_id=str(request.state.tenant_id),
        external_user_id=_public_tenant_mcp_external_user_id(request),
    )
    return MemoryDeleteResponse(data=MemoryDeleteData(deleted=deleted), request_id=get_request_id(request), timestamp=utc_now())


@router.get("/tenant/clarifications", response_model=TenantMCPClarificationListResponse)
async def clarifications_for_public_tenant_mcp(
    request: Request,
    session: DbSession,
    proxy_user_service: Annotated[ProxyUserService, Depends(get_proxy_user_service)],
) -> TenantMCPClarificationListResponse:
    """List unresolved, self-owned conflict choices for the signed-in caller.

    This deliberately excludes generic queue questions and every conflict whose
    two source memories are not both owned by this exact public-MCP profile.
    """
    proxy_user = await _public_tenant_mcp_proxy_user(
        request=request,
        proxy_user_service=proxy_user_service,
    )
    clarifications = (
        await session.execute(
            select(ClarificationQueue)
            .options(
                selectinload(ClarificationQueue.conflict).selectinload(CrossUserConflict.user_a_memory),
                selectinload(ClarificationQueue.conflict).selectinload(CrossUserConflict.user_b_memory),
            )
            .where(
                ClarificationQueue.tenant_id == request.state.tenant_id,
                ClarificationQueue.proxy_user_id == proxy_user.id,
                ClarificationQueue.status.in_(
                    [ClarificationQueueStatus.pending, ClarificationQueueStatus.triggered]
                ),
                ClarificationQueue.expires_at > utc_now(),
            )
            .order_by(ClarificationQueue.created_at.asc(), ClarificationQueue.id.asc())
        )
    ).scalars().all()
    items = [
        TenantMCPClarificationItem(
            id=item.id,
            question_context=item.question_context,
            value_a=item.conflict.entity_value_a,
            value_b=item.conflict.entity_value_b,
            entity_type=(
                item.conflict.entity_type.value
                if hasattr(item.conflict.entity_type, "value")
                else str(item.conflict.entity_type)
            ),
            created_at=item.created_at,
            expires_at=item.expires_at,
            status=item.status.value if hasattr(item.status, "value") else str(item.status),
        )
        for item in clarifications
        if _is_self_scoped_public_clarification(item, proxy_user_id=proxy_user.id)
    ]
    return TenantMCPClarificationListResponse(
        data=TenantMCPClarificationListData(clarifications=items),
        request_id=get_request_id(request),
        timestamp=utc_now(),
    )


@router.post(
    "/tenant/clarifications/{clarification_id}/answer",
    response_model=TenantMCPClarificationAnswerResponse,
)
async def answer_clarification_for_public_tenant_mcp(
    request: Request,
    clarification_id: str,
    payload: TenantMCPClarificationAnswerRequest,
    session: DbSession,
    proxy_user_service: Annotated[ProxyUserService, Depends(get_proxy_user_service)],
) -> TenantMCPClarificationAnswerResponse:
    """Resolve one self-owned clarification after the user explicitly chooses."""
    try:
        parsed_id = UUID(clarification_id)
    except ValueError as exc:
        raise APIError(status_code=404, code="CLR_404", error="clarification_not_found") from exc

    proxy_user = await _public_tenant_mcp_proxy_user(
        request=request,
        proxy_user_service=proxy_user_service,
    )
    clarification = (
        await session.execute(
            select(ClarificationQueue)
            .options(
                selectinload(ClarificationQueue.conflict).selectinload(CrossUserConflict.user_a_memory),
                selectinload(ClarificationQueue.conflict).selectinload(CrossUserConflict.user_b_memory),
            )
            .where(
                ClarificationQueue.id == parsed_id,
                ClarificationQueue.tenant_id == request.state.tenant_id,
                ClarificationQueue.proxy_user_id == proxy_user.id,
                ClarificationQueue.status.in_(
                    [ClarificationQueueStatus.pending, ClarificationQueueStatus.triggered]
                ),
                ClarificationQueue.expires_at > utc_now(),
            )
        )
    ).scalar_one_or_none()
    if clarification is None or not _is_self_scoped_public_clarification(
        clarification,
        proxy_user_id=proxy_user.id,
    ):
        raise APIError(status_code=404, code="CLR_404", error="clarification_not_found")

    conflict = clarification.conflict
    if conflict.status in {CrossUserConflictStatus.resolved, CrossUserConflictStatus.ignored}:
        raise APIError(status_code=409, code="CLR_409", error="clarification_already_resolved")

    default_reasons = {
        "A": "User confirmed memory A.",
        "B": "User confirmed memory B.",
        "both": "User said both versions are correct.",
        "neither": "User said neither version is correct.",
    }
    reason = payload.reason or default_reasons[payload.answer]
    try:
        await apply_conflict_selection(
            session,
            conflict=conflict,
            selection=payload.answer,
            changed_by="user",
            reason=reason,
        )
    except ValueError as exc:
        raise APIError(status_code=400, code="CLR_400", error=str(exc)) from exc

    conflict.status = (
        CrossUserConflictStatus.ignored
        if payload.answer == "neither"
        else CrossUserConflictStatus.resolved
    )
    conflict.resolved_at = datetime.now(UTC)
    conflict.resolved_by = "user_session"
    conflict.resolution = "both_valid" if payload.answer == "both" else payload.answer
    conflict.resolution_reason = reason
    conflict.requires_attention = False
    clarification.status = ClarificationQueueStatus.resolved
    await session.commit()
    return TenantMCPClarificationAnswerResponse(
        data=TenantMCPClarificationAnswerData(resolved=True, clarification_id=clarification.id),
        request_id=get_request_id(request),
        timestamp=utc_now(),
    )


@router.post("/universal/capability")
async def issue_universal_capability(
    request: Request,
    session: DbSession,
    cache_service: Annotated[CacheService, Depends(get_cache_service)],
    x_memoryos_mcp_universal_agent_key: Annotated[
        str | None, Header(alias="X-MemoryOS-MCP-Universal-Agent-Key")
    ] = None,
) -> dict[str, object]:
    """Exchange a verified public-MCP Clerk session for a scoped Universal capability."""
    if request.headers.get("x-memoryos-mcp-client", "").strip().lower() != "public-v1":
        raise APIError(status_code=403, code="MCP_403", error="public_mcp_marker_required")
    email = str(getattr(request.state, "auth_email", "") or "").strip().lower()
    if not email or not bool(getattr(request.state, "auth_email_verified", False)):
        raise APIError(status_code=403, code="MCP_403", error="verified_email_required")
    agent = await GlobalAgentService(session=session, cache_service=cache_service).resolve_from_api_key(
        str(x_memoryos_mcp_universal_agent_key or "").strip()
    )
    if agent is None:
        raise APIError(status_code=403, code="MCP_403", error="mcp_agent_auth_failed")
    uui_service = UUIService(session=session, cache_service=cache_service)
    universal_user = await uui_service.resolve_by_email(email)
    if universal_user is None:
        raise APIError(status_code=403, code="MCP_403", error="universal_profile_required")
    grants = await uui_service.get_grants(str(universal_user.id))
    grant = next((item for item in grants if str(item.agent_id) == str(agent.id)), None)
    if grant is None:
        raise APIError(status_code=403, code="MCP_403", error="universal_consent_required")
    try:
        capability = issue_universal_mcp_capability(
            user_uui_id=str(universal_user.id), agent_id=str(agent.id), grant_id=str(grant.id)
        )
    except ValueError as exc:
        raise APIError(status_code=503, code="MCP_503", error="universal_capability_unavailable") from exc
    return {
        "data": {
            "capability": capability,
            "expires_in": 300,
            "access_type": grant.access_type,
            "categories_allowed": list(grant.categories_allowed or []),
        }
    }
