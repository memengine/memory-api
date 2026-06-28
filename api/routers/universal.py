from __future__ import annotations

import uuid
from datetime import UTC
from datetime import datetime
from typing import Annotated
from typing import Any

from fastapi import APIRouter
from fastapi import Depends
from fastapi import Request
from fastapi import Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from pydantic import Field
from celery.result import AsyncResult
from qdrant_client.http import models as qmodels
from sqlalchemy import select

from api.celery_app import celery_app
from api.dependencies import DbSession
from api.dependencies import get_cache_service
from api.dependencies import get_context_builder
from api.dependencies import get_qdrant_service
from api.dependencies import get_quality_gate_service
from api.db.cache import CacheService
from api.db.models import PermissionGrant
from api.db.models import UniversalMemory
from api.db.models import UniversalUser
from api.db.vector_store import QdrantService
from api.routers.common import get_request_id
from api.routers.common import utc_now
from api.schemas.requests import ConversationMessageRequest
from api.schemas.requests import MemoryFormat
from api.schemas.responses import MemoryAddResponse
from api.schemas.responses import MemoryRetrieveResponse
from api.schemas.responses import MemorySearchResult
from api.services.context_builder import ContextBuilder
from api.services.embedding_service import EmbeddingService
from api.services.quality_gate import QualityGateService
from api.services.retriever import MemoryResult
from api.services.retriever import RetrieverService
from api.services.uui_service import UUIService


UNIVERSAL_EXTRACTION_TASK_NAME = "api.tasks.universal_extraction_tasks.extract_universal_memory"
UNIVERSAL_COLLECTION_NAME = "universal_memories"

router = APIRouter(prefix="/v1/universal", tags=["universal"])


class UniversalMemoryAddRequest(BaseModel):
    messages: list[ConversationMessageRequest] = Field(min_length=1)
    metadata: dict[str, Any] = Field(default_factory=dict)
    idempotency_key: str | None = None


class UniversalMemoryRetrieveRequest(BaseModel):
    query: str = Field(min_length=1)
    limit: int = Field(default=10, ge=1, le=50)
    format: MemoryFormat = "bullets"
    context_max_tokens: int = Field(default=500, ge=50, le=4000)


class UniversalMemoryRetrieveResponse(MemoryRetrieveResponse):
    permission_error: str | None = None
    categories_available: list[str] = Field(default_factory=list)
    is_passthrough: bool = False


class UniversalMemoryJobStatusResponse(BaseModel):
    job_id: str
    state: str
    status: str
    memories_created: int | None = None
    blocked_reason: str | None = None
    error: str | None = None
    result: dict[str, Any] | None = None


def _current_global_agent(request: Request):
    agent = getattr(request.state, "global_agent", None)
    if agent is None:
        raise RuntimeError("Universal auth context missing global_agent.")
    return agent


def _current_universal_user(request: Request):
    universal_user = getattr(request.state, "universal_user", None)
    if universal_user is None:
        raise RuntimeError("Universal auth context missing universal_user.")
    return universal_user


async def _get_active_grant(
    session: DbSession,
    *,
    user_uui_id: str,
    agent_id: str,
) -> PermissionGrant | None:
    result = await session.execute(
        select(PermissionGrant).where(
            PermissionGrant.user_uui_id == uuid.UUID(user_uui_id),
            PermissionGrant.agent_id == uuid.UUID(agent_id),
            PermissionGrant.is_active.is_(True),
            (PermissionGrant.expires_at.is_(None) | (PermissionGrant.expires_at > datetime.now(UTC))),
        )
    )
    return result.scalar_one_or_none()


async def _dispatch_universal_job(job_payload: dict[str, Any]) -> str | None:
    try:
        dispatched = celery_app.send_task(
            UNIVERSAL_EXTRACTION_TASK_NAME,
            args=[job_payload],
            task_id=str(job_payload.get("job_id")),
        )
        if dispatched is not None and hasattr(dispatched, "__await__"):
            await dispatched
        return None
    except Exception:
        return "dispatch_failed"


def _merge_scored_points(points: list[Any]) -> list[Any]:
    merged: dict[str, Any] = {}
    for point in points:
        point_id = str(getattr(point, "id", ""))
        if not point_id:
            continue
        existing = merged.get(point_id)
        if existing is None or float(getattr(point, "score", 0.0) or 0.0) > float(
            getattr(existing, "score", 0.0) or 0.0
        ):
            merged[point_id] = point
    return sorted(
        merged.values(),
        key=lambda item: float(getattr(item, "score", 0.0) or 0.0),
        reverse=True,
    )


async def _search_universal_memories(
    *,
    session: DbSession,
    qdrant_service: QdrantService,
    context_builder: ContextBuilder,
    query: str,
    user: UniversalUser,
    allowed_categories: list[str],
    limit: int,
    format: str,
    context_max_tokens: int,
) -> tuple[list[MemorySearchResult], str, int]:
    if not allowed_categories:
        return [], "", 0

    embedder = EmbeddingService(async_session=session)
    query_embedding = await embedder.embed(query)
    qdrant_service._ensure_collection_if_possible(
        collection_name=UNIVERSAL_COLLECTION_NAME,
        vector_size=query_embedding.dimensions,
    )

    scored_points: list[Any] = []
    for category in allowed_categories:
        response = qdrant_service.client.query_points(
            collection_name=UNIVERSAL_COLLECTION_NAME,
            query=query_embedding.vector,
            query_filter=qmodels.Filter(
                must=[
                    qmodels.FieldCondition(
                        key="user_uui_id",
                        match=qmodels.MatchValue(value=str(user.id)),
                    ),
                    qmodels.FieldCondition(
                        key="category",
                        match=qmodels.MatchValue(value=category),
                    ),
                    qmodels.FieldCondition(
                        key="is_archived",
                        match=qmodels.MatchValue(value=False),
                    ),
                ]
            ),
            limit=max(limit * 4, limit),
            with_payload=True,
            with_vectors=False,
        )
        scored_points.extend(response if isinstance(response, list) else list(response.points))

    ranked_points = _merge_scored_points(scored_points)
    if not ranked_points:
        return [], "", 0

    memory_ids = [str(getattr(point, "id", "")) for point in ranked_points if getattr(point, "id", None)]
    result = await session.execute(
        select(UniversalMemory).where(
            UniversalMemory.id.in_([uuid.UUID(memory_id) for memory_id in memory_ids]),
            UniversalMemory.user_uui_id == user.id,
            UniversalMemory.is_archived.is_(False),
        )
    )
    memories = {str(memory.id): memory for memory in result.scalars().all()}

    scored_results: list[MemoryResult] = []
    now = datetime.now(UTC)
    for point in ranked_points:
        memory_id = str(getattr(point, "id", ""))
        memory = memories.get(memory_id)
        if memory is None:
            continue
        semantic_score = float(getattr(point, "score", 0.0) or 0.0)
        recency_score = RetrieverService._recency_score(memory.last_accessed_at)
        final_score = (
            (0.60 * semantic_score)
            + (0.25 * (float(memory.importance_score) / 10.0))
            + (0.15 * recency_score)
        )
        memory.last_accessed_at = now
        scored_results.append(
            MemoryResult(
                id=str(memory.id),
                content=memory.content,
                category=memory.category.value if hasattr(memory.category, "value") else str(memory.category),
                importance_score=float(memory.importance_score),
                confidence_score=float(memory.confidence),
                semantic_score=semantic_score,
                recency_score=recency_score,
                final_score=round(final_score, 6),
                agent_id=str(memory.source_agent_id),
                previous_version_id=None,
                last_accessed_at=memory.last_accessed_at.isoformat() if memory.last_accessed_at else None,
                created_at=memory.created_at.isoformat() if memory.created_at else None,
            )
        )

    deduplicated = RetrieverService._deduplicate_results(scored_results)
    final_results = sorted(deduplicated, key=lambda item: item.final_score, reverse=True)[:limit]
    await session.commit()

    data = [
        MemorySearchResult(
            id=item.id,
            content=item.content,
            category=item.category,
            importance_score=item.importance_score,
            last_accessed=datetime.fromisoformat(item.last_accessed_at) if item.last_accessed_at else None,
            relevance_score=item.final_score,
            context_snippet=context_builder.build_context([item], format=format, max_tokens=120),
        )
        for item in final_results
    ]
    context = context_builder.build(
        final_results,
        format=format,
        max_tokens=context_max_tokens,
    )
    return data, context.system_prompt_addition, context.token_count


@router.post("/memories/add", response_model=MemoryAddResponse)
async def add_universal_memories(
    request: Request,
    response: Response,
    payload: UniversalMemoryAddRequest,
    session: DbSession,
    cache_service: Annotated[CacheService, Depends(get_cache_service)],
    quality_gate_service: Annotated[QualityGateService, Depends(get_quality_gate_service)],
) -> MemoryAddResponse:
    agent = _current_global_agent(request)
    user = _current_universal_user(request)
    uui_service = UUIService(session=session, cache_service=cache_service)
    grants = await uui_service.get_grants(str(user.id))
    grant = next((item for item in grants if str(item.agent_id) == str(agent.id)), None)
    if grant is None or str(getattr(grant, "access_type", "")) != "read_write":
        return JSONResponse(
            status_code=403,
            content={
                "error": "write_not_permitted",
                "code": "UAT_002",
                "request_id": get_request_id(request),
            },
        )

    idempotency_scope = f"universal:{agent.id}:{user.id}"
    if payload.idempotency_key:
        cached_job = await cache_service.get_idempotent_response(
            payload.idempotency_key,
            scope=idempotency_scope,
            operation="universal_memory_add",
        )
        if cached_job is not None:
            return MemoryAddResponse(
                job_id=cached_job.get("job_id"),
                status=str(cached_job.get("status") or "queued"),
                blocked_reason=cached_job.get("blocked_reason"),
                retry_after_seconds=cached_job.get("retry_after_seconds"),
                budget_remaining_pct=cached_job.get("budget_remaining_pct"),
                processing_eta_seconds=cached_job.get("processing_eta_seconds"),
                processing_status=cached_job.get("processing_status") or "normal",
                request_id=get_request_id(request),
                timestamp=utc_now(),
            )

    gate_result = await quality_gate_service.check(
        [message.model_dump() for message in payload.messages],
        str(agent.owner_tenant_id),
        str(user.id),
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

    job_payload = {
        "job_id": str(uuid.uuid4()),
        "status": "queued",
        "user_uui_id": str(user.id),
        "agent_id": str(agent.id),
        "owner_tenant_id": str(agent.owner_tenant_id),
        "messages": [message.model_dump() for message in payload.messages],
        "metadata": payload.metadata,
        "queued_at": utc_now().isoformat(),
        "processing_status": "normal",
    }
    if payload.idempotency_key:
        await cache_service.set_idempotent_response(
            payload.idempotency_key,
            job_payload,
            ttl=86400,
            scope=idempotency_scope,
            operation="universal_memory_add",
        )
    dispatch_error = await _dispatch_universal_job(job_payload)
    if dispatch_error:
        if payload.idempotency_key:
            failed_payload = {
                **job_payload,
                "status": "error",
                "blocked_reason": dispatch_error,
            }
            await cache_service.set_idempotent_response(
                payload.idempotency_key,
                failed_payload,
                ttl=300,
                scope=idempotency_scope,
                operation="universal_memory_add",
            )
        return JSONResponse(
            status_code=200,
            content={
                "job_id": job_payload["job_id"],
                "status": "error",
                "blocked_reason": dispatch_error,
                "retry_after_seconds": None,
                "budget_remaining_pct": gate_result.budget_remaining_pct,
                "request_id": get_request_id(request),
                "timestamp": utc_now().isoformat(),
            },
        )

    response.headers["X-MemoryOS-Processing"] = "normal"
    return MemoryAddResponse(
        job_id=job_payload["job_id"],
        status="queued",
        blocked_reason=None,
        retry_after_seconds=None,
        budget_remaining_pct=gate_result.budget_remaining_pct,
        processing_eta_seconds=None,
        processing_status="normal",
        request_id=get_request_id(request),
        timestamp=utc_now(),
    )


@router.get("/memories/jobs/{job_id}", response_model=UniversalMemoryJobStatusResponse)
async def get_universal_memory_job_status(
    job_id: str,
) -> UniversalMemoryJobStatusResponse:
    result = AsyncResult(job_id, app=celery_app)
    payload = result.result if isinstance(result.result, dict) else None
    if payload is None:
        return UniversalMemoryJobStatusResponse(
            job_id=job_id,
            state=result.state,
            status=str(result.state).lower(),
            memories_created=None,
            blocked_reason=None,
            error=str(result.result) if result.failed() and result.result is not None else None,
            result=None,
        )

    return UniversalMemoryJobStatusResponse(
        job_id=job_id,
        state=result.state,
        status=str(payload.get("status") or result.state).lower(),
        memories_created=payload.get("memories_created"),
        blocked_reason=payload.get("blocked_reason"),
        error=payload.get("error"),
        result=payload,
    )


@router.post("/memories/retrieve", response_model=UniversalMemoryRetrieveResponse)
async def retrieve_universal_memories(
    request: Request,
    payload: UniversalMemoryRetrieveRequest,
    session: DbSession,
    cache_service: Annotated[CacheService, Depends(get_cache_service)],
    qdrant_service: Annotated[QdrantService, Depends(get_qdrant_service)],
    context_builder: Annotated[ContextBuilder, Depends(get_context_builder)],
) -> UniversalMemoryRetrieveResponse:
    agent = _current_global_agent(request)
    user = _current_universal_user(request)

    uui_service = UUIService(session=session, cache_service=cache_service)
    grants = await uui_service.get_grants(str(user.id))
    grant = next((item for item in grants if str(item.agent_id) == str(agent.id)), None)
    if grant is None:
        return UniversalMemoryRetrieveResponse(
            data=[],
            cached=False,
            system_prompt_addition="",
            context_token_count=0,
            permission_error="no_grant_for_user",
            categories_available=[],
            is_passthrough=False,
            request_id=get_request_id(request),
            timestamp=utc_now(),
        )

    categories_available = list(grant.categories_allowed or [])
    data, system_prompt_addition, context_token_count = await _search_universal_memories(
        session=session,
        qdrant_service=qdrant_service,
        context_builder=context_builder,
        query=payload.query,
        user=user,
        allowed_categories=categories_available,
        limit=payload.limit,
        format=payload.format,
        context_max_tokens=payload.context_max_tokens,
    )
    return UniversalMemoryRetrieveResponse(
        data=data,
        cached=False,
        system_prompt_addition=system_prompt_addition,
        context_token_count=context_token_count,
        permission_error=None,
        categories_available=categories_available,
        is_passthrough=False,
        request_id=get_request_id(request),
        timestamp=utc_now(),
    )
