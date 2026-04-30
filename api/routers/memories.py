from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter
from fastapi import Depends
from fastapi import Header
from fastapi import Query
from fastapi import Request
from fastapi import Response
from fastapi.responses import JSONResponse

from api.dependencies import get_authenticated_user_id
from api.dependencies import get_authenticated_tenant_id
from api.dependencies import get_context_builder
from api.dependencies import get_db_session
from api.dependencies import get_memory_service
from api.dependencies import get_proxy_user_service
from api.dependencies import get_quality_gate_service
from api.dependencies import get_retriever_service
from api.schemas.requests import MemoryAddRequest
from api.schemas.requests import MemoryRetrieveRequest
from api.schemas.requests import MemoryUpdateRequest
from api.schemas.responses import CursorPage
from api.schemas.responses import MemoryAddResponse
from api.schemas.responses import MemoryData
from api.schemas.responses import MemoryDeleteData
from api.schemas.responses import MemoryDeleteResponse
from api.schemas.responses import MemoryGetResponse
from api.schemas.responses import MemoryJobStatusData
from api.schemas.responses import MemoryJobStatusResponse
from api.schemas.responses import MemoryListResponse
from api.schemas.responses import MemoryMutationResponse
from api.schemas.responses import MemoryRetrieveResponse
from api.schemas.responses import MemorySearchResult
from api.errors import APIError
from api.services.context_builder import ContextBuilder
from api.services.memory_service import MemoryService
from api.services.proxy_user_service import ProxyUserService
from api.services.quality_gate import QualityGateService
from api.services.retriever import RetrieverService
from api.services.version_service import VersionService
from api.routers.common import get_request_id
from api.routers.common import utc_now
from api.tasks.queue_router import get_processing_eta
from sqlalchemy.ext.asyncio import AsyncSession


router = APIRouter(prefix="/v1/memories", tags=["memories"])


def _memory_to_data(memory) -> MemoryData:
    return MemoryData(
        id=str(memory.id),
        content=memory.content,
        category=memory.category.value,
        importance_score=float(memory.importance_score),
        confidence_score=float(memory.confidence_score),
        created_at=memory.created_at,
        updated_at=memory.updated_at,
        last_accessed_at=memory.last_accessed_at,
        access_count=int(memory.access_count or 0),
        is_archived=bool(memory.is_archived),
        agent_id=str(memory.agent_id) if memory.agent_id else None,
        previous_version_id=str(memory.previous_version_id) if memory.previous_version_id else None,
        source_conversation_id=str(memory.source_conversation_id) if memory.source_conversation_id else None,
        metadata=memory.metadata_json or {},
    )


def _memory_scope(request: Request) -> tuple[str | None, str | None]:
    tenant_id = getattr(request.state, "tenant_id", None)
    authenticated_user_id = getattr(request.state, "user_id", None)
    return (
        str(tenant_id) if tenant_id else None,
        str(authenticated_user_id) if authenticated_user_id else None,
    )


@router.get("", response_model=MemoryListResponse)
async def list_memories(
    request: Request,
    memory_service: Annotated[MemoryService, Depends(get_memory_service)],
    cursor: str | None = Query(default=None, description="Cursor from the previous page."),
    limit: int = Query(default=10, ge=1, le=50, description="Maximum number of memories to return."),
    categories: list[str] = Query(default_factory=list, description="Optional category filters."),
    agent_id: str | None = Query(default=None, description="Optional agent identifier."),
    external_user_id: str | None = Query(
        default=None,
        description="Optional external user identifier filter for tenant-scoped requests.",
    ),
) -> MemoryListResponse:
    """List memories for the authenticated user.

    Parameters: cursor for pagination, limit up to 50, optional category filters, optional agent filter.
    Responses: paginated memory list wrapped in the standard envelope.
    """
    tenant_id, authenticated_user_id = _memory_scope(request)
    memories, next_cursor, total = await memory_service.list_memories(
        requested_user_id=None,
        authenticated_user_id=authenticated_user_id,
        tenant_id=tenant_id,
        cursor=cursor,
        limit=limit,
        categories=categories,
        agent_id=agent_id,
        external_user_id=external_user_id,
    )
    return MemoryListResponse(
        data=[_memory_to_data(memory) for memory in memories],
        pagination=CursorPage(next_cursor=next_cursor, limit=limit, total=total),
        request_id=get_request_id(request),
        timestamp=utc_now(),
    )


@router.post("/add", response_model=MemoryAddResponse)
async def add_memories(
    request: Request,
    response: Response,
    payload: MemoryAddRequest,
    memory_service: Annotated[MemoryService, Depends(get_memory_service)],
    proxy_user_service: Annotated[ProxyUserService, Depends(get_proxy_user_service)],
    quality_gate_service: Annotated[QualityGateService, Depends(get_quality_gate_service)],
    tenant_id: str = Depends(get_authenticated_tenant_id),
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> MemoryAddResponse:
    """Queue conversation ingestion for memory extraction.

    Parameters: external_user_id, optional agent_id, conversation messages, optional metadata, optional Idempotency-Key header.
    Responses: queued job metadata or a blocked response with gate metadata for B2B callers.
    """
    gate_result = await quality_gate_service.check(
        [message.model_dump() for message in payload.messages],
        tenant_id,
        payload.external_user_id,
    )
    if not gate_result.passed:
        return JSONResponse(
            status_code=200,
            content={
                "job_id": None,
                "status": gate_result.blocked_layer or "blocked",
                "blocked_reason": gate_result.reason,
                "retry_after_seconds": gate_result.retry_after_seconds,
                "budget_remaining_pct": gate_result.budget_remaining_pct,
                "request_id": get_request_id(request),
                "timestamp": utc_now().isoformat(),
            },
        )

    proxy_user = await proxy_user_service.resolve(
        tenant_id=tenant_id,
        external_user_id=payload.external_user_id,
        metadata=payload.metadata,
    )
    request.state.proxy_user_id = str(proxy_user.id)

    job = await memory_service.queue_memory_add(
        requested_user_id=None,
        authenticated_user_id=None,
        agent_id=payload.agent_id,
        messages=[message.model_dump() for message in payload.messages],
        metadata=payload.metadata,
        idempotency_key=idempotency_key,
        tenant_id=tenant_id,
        external_user_id=payload.external_user_id,
        proxy_user_id=str(proxy_user.id),
    )
    if job.get("status") != "queued":
        return JSONResponse(
            status_code=200,
            content={
                "job_id": job.get("job_id"),
                "status": job.get("status"),
                "blocked_reason": job.get("blocked_reason"),
                "retry_after_seconds": None,
                "budget_remaining_pct": job.get(
                    "budget_remaining_pct",
                    gate_result.budget_remaining_pct,
                ),
                "request_id": get_request_id(request),
                "timestamp": utc_now().isoformat(),
            },
        )

    processing_eta_seconds = await asyncio.to_thread(get_processing_eta, tenant_id)
    processing_status = "delayed" if processing_eta_seconds is not None else "normal"
    response.headers["X-MemoryOS-Processing"] = processing_status
    return MemoryAddResponse(
        job_id=job["job_id"],
        status=job["status"],
        blocked_reason=job.get("blocked_reason"),
        retry_after_seconds=None,
        budget_remaining_pct=job.get(
            "budget_remaining_pct",
            gate_result.budget_remaining_pct,
        ),
        processing_eta_seconds=processing_eta_seconds,
        processing_status=processing_status,
        request_id=get_request_id(request),
        timestamp=utc_now(),
    )


@router.post("/retrieve", response_model=MemoryRetrieveResponse)
async def retrieve_memories(
    request: Request,
    payload: MemoryRetrieveRequest,
    retriever_service: Annotated[RetrieverService, Depends(get_retriever_service)],
    proxy_user_service: Annotated[ProxyUserService, Depends(get_proxy_user_service)],
    context_builder: Annotated[ContextBuilder, Depends(get_context_builder)],
    tenant_id: str = Depends(get_authenticated_tenant_id),
) -> MemoryRetrieveResponse:
    """Retrieve semantically relevant memories for a query.

    Parameters: external_user_id, natural language query, optional limit, category filters, agent filter, and context format.
    Responses: ranked memory results, cache flag, and a prompt-ready memory context section.
    """
    proxy_user = await proxy_user_service.resolve(
        tenant_id=tenant_id,
        external_user_id=payload.external_user_id,
    )
    request.state.proxy_user_id = str(proxy_user.id)
    results = await retriever_service.retrieve(
        query=payload.query,
        external_user_id=payload.external_user_id,
        proxy_user_id=str(proxy_user.id),
        limit=payload.limit,
        categories=list(payload.categories),
        agent_id=payload.agent_id,
        tenant_id=tenant_id,
    )
    data = [
        MemorySearchResult(
            id=result.id,
            content=result.content,
            category=result.category,
            importance_score=result.importance_score,
            last_accessed=datetime.fromisoformat(result.last_accessed_at) if result.last_accessed_at else None,
            relevance_score=result.final_score,
            context_snippet=context_builder.build_context([result], format=payload.format, max_tokens=120),
        )
        for result in results
    ]
    context = context_builder.build(
        results,
        format=payload.format,
        max_tokens=payload.context_max_tokens,
    )
    return MemoryRetrieveResponse(
        data=data,
        cached=bool(retriever_service.last_cache_hit),
        system_prompt_addition=context.system_prompt_addition,
        context_token_count=context.token_count,
        request_id=get_request_id(request),
        timestamp=utc_now(),
    )


@router.get("/{memory_id}/history")
async def get_memory_history(
    request: Request,
    memory_id: str,
    session: Annotated[AsyncSession, Depends(get_db_session)],
    tenant_id: str = Depends(get_authenticated_tenant_id),
) -> dict[str, object]:
    """Fetch append-only version history for a tenant-owned memory."""
    try:
        versions = await VersionService(session).get_history(memory_id=memory_id, tenant_id=tenant_id)
    except PermissionError as exc:
        raise APIError(
            status_code=404,
            code="MEM_404",
            error="memory_not_found",
            details={"memory_id": memory_id},
        ) from exc

    return {
        "data": [
            {
                "version_number": version.version_number,
                "content": version.content,
                "change_type": version.change_type,
                "change_reason": version.change_reason,
                "created_at": version.created_at,
            }
            for version in versions
        ],
        "request_id": get_request_id(request),
        "timestamp": utc_now(),
    }


@router.get("/{memory_id}", response_model=MemoryGetResponse)
async def get_memory(
    request: Request,
    memory_id: str,
    memory_service: Annotated[MemoryService, Depends(get_memory_service)],
) -> MemoryGetResponse:
    """Fetch a single memory by id.

    Parameters: memory id from the path.
    Responses: a single memory record with metadata and history pointers.
    """
    tenant_id, authenticated_user_id = _memory_scope(request)
    memory = await memory_service.get_memory(
        authenticated_user_id=authenticated_user_id,
        memory_id=memory_id,
        tenant_id=tenant_id,
    )
    return MemoryGetResponse(
        data=_memory_to_data(memory),
        request_id=get_request_id(request),
        timestamp=utc_now(),
    )


@router.patch("/{memory_id}", response_model=MemoryMutationResponse)
async def update_memory(
    request: Request,
    memory_id: str,
    payload: MemoryUpdateRequest,
    memory_service: Annotated[MemoryService, Depends(get_memory_service)],
) -> MemoryMutationResponse:
    """Update memory content or importance.

    Parameters: memory id and optional content, importance_score, or archive state updates.
    Responses: the updated memory envelope.
    """
    tenant_id, authenticated_user_id = _memory_scope(request)
    memory = await memory_service.update_memory(
        authenticated_user_id=authenticated_user_id,
        memory_id=memory_id,
        content=payload.content,
        importance_score=payload.importance_score,
        is_archived=payload.is_archived,
        tenant_id=tenant_id,
    )
    return MemoryMutationResponse(
        data=_memory_to_data(memory),
        request_id=get_request_id(request),
        timestamp=utc_now(),
    )


@router.delete("/{memory_id}", response_model=MemoryDeleteResponse)
async def delete_memory(
    request: Request,
    memory_id: str,
    memory_service: Annotated[MemoryService, Depends(get_memory_service)],
    hard_delete: bool = Query(default=False, description="Permanently delete instead of archiving."),
) -> MemoryDeleteResponse:
    """Delete or archive a memory.

    Parameters: memory id and optional hard_delete query flag.
    Responses: deletion status wrapped in the standard envelope.
    """
    tenant_id, authenticated_user_id = _memory_scope(request)
    deleted = await memory_service.delete_memory(
        authenticated_user_id=authenticated_user_id,
        memory_id=memory_id,
        hard_delete=hard_delete,
        tenant_id=tenant_id,
    )
    return MemoryDeleteResponse(
        data=MemoryDeleteData(deleted=deleted),
        request_id=get_request_id(request),
        timestamp=utc_now(),
    )


@router.get("/jobs/{job_id}", response_model=MemoryJobStatusResponse)
async def get_memory_job_status(
    request: Request,
    job_id: str,
    memory_service: Annotated[MemoryService, Depends(get_memory_service)],
) -> MemoryJobStatusResponse:
    """Fetch the status of an extraction job.

    Parameters: job id from the path.
    Responses: job id, current status, and number of memories created so far.
    """
    authenticated_user_id = getattr(request.state, "user_id", None)
    tenant_id = getattr(request.state, "tenant_id", None)
    if not authenticated_user_id and not tenant_id:
        authenticated_user_id = get_authenticated_user_id(request)
    job = await memory_service.get_job_status(job_id=job_id)
    if tenant_id and job.get("tenant_id") not in {None, str(tenant_id)}:
        raise APIError(status_code=404, code="JOB_404", error="job_not_found")
    return MemoryJobStatusResponse(
        data=MemoryJobStatusData(
            job_id=job["job_id"],
            status=job["status"],
            memories_created=int(job.get("memories_created", 0)),
            attempts=int(job.get("attempts", 0)),
            created_at=datetime.fromisoformat(job["created_at"]) if job.get("created_at") else None,
            processing_started_at=datetime.fromisoformat(job["processing_started_at"])
            if job.get("processing_started_at")
            else None,
            queue_name=job.get("queue_name"),
            error=job.get("error"),
            error_summary=job.get("error_summary"),
            queued_at=datetime.fromisoformat(job["queued_at"]) if job.get("queued_at") else None,
            started_at=datetime.fromisoformat(job["started_at"]) if job.get("started_at") else None,
            completed_at=datetime.fromisoformat(job["completed_at"]) if job.get("completed_at") else None,
            dead_lettered_at=datetime.fromisoformat(job["dead_lettered_at"]) if job.get("dead_lettered_at") else None,
        ),
        request_id=get_request_id(request),
        timestamp=utc_now(),
    )
