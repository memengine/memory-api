from __future__ import annotations

import logging
import asyncio
import uuid
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter
from fastapi import Depends
from fastapi import Header
from fastapi import Query
from fastapi import Request
from fastapi import Response
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from api.dependencies import get_authenticated_user_id
from api.dependencies import get_authenticated_tenant_id
from api.dependencies import get_context_builder
from api.dependencies import get_cache_service
from api.dependencies import get_db_session
from api.dependencies import get_memory_service
from api.dependencies import get_proxy_user_service
from api.dependencies import get_quality_gate_service
from api.dependencies import get_retriever_service
from api.db.models import ClarificationQueue
from api.db.models import ClarificationQueueStatus
from api.db.models import EdTechMemory
from api.db.models import Tenant
from api.db.cache import CacheService
from api.schemas.requests import MemoryAddRequest
from api.schemas.requests import MemoryRetrieveRequest
from api.schemas.requests import MemoryUpdateRequest
from api.schemas.requests import RetrievalFeedbackRequest
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
from api.schemas.responses import RetrievalFeedbackData
from api.schemas.responses import RetrievalFeedbackResponse
from api.schemas.edtech_schemas import EdTechMemoryView
from api.schemas.edtech_schemas import EdTechProfileResponse
from api.errors import APIError
from api.services.context_builder import ContextBuilder
from api.services.domain_schemas.registry import get_domain_schema
from api.services.memory_service import MemoryService
from api.services.proxy_user_service import ProxyUserService
from api.services.quality_gate import QualityGateService
from api.services.retriever import RetrieverService
from api.services.retrieval_feedback_service import RetrievalFeedbackService
from api.services.version_service import VersionService
from api.routers.common import get_request_id
from api.routers.common import utc_now
from api.tasks.queue_router import get_processing_eta


router = APIRouter(prefix="/v1/memories", tags=["memories"])
logger = logging.getLogger(__name__)


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
        source_event_id=str(getattr(memory, "source_event_id", None)) if getattr(memory, "source_event_id", None) else None,
        provenance=(getattr(memory, "metadata_json", None) or {}).get("provenance"),
        metadata=memory.metadata_json or {},
    )


def _memory_scope(request: Request) -> tuple[str | None, str | None]:
    tenant_id = getattr(request.state, "tenant_id", None)
    authenticated_user_id = getattr(request.state, "user_id", None)
    return (
        str(tenant_id) if tenant_id else None,
        str(authenticated_user_id) if authenticated_user_id else None,
    )


async def _tenant_domain_schema(session: AsyncSession, tenant_id: str) -> str | None:
    tenant = await session.get(Tenant, uuid.UUID(str(tenant_id)))
    if tenant is None:
        return None
    metadata = tenant.metadata_json or {}
    if metadata.get("edtech_schema_enabled") and not metadata.get("domain_schema"):
        return "edtech"
    return metadata.get("domain_schema") or metadata.get("memory_domain")


def _edtech_memory_to_view(memory: EdTechMemory) -> EdTechMemoryView:
    return EdTechMemoryView(
        id=str(memory.id),
        proxy_user_id=str(memory.proxy_user_id),
        tenant_id=str(memory.tenant_id),
        learner_type=memory.learner_type,
        learner_type_confidence=memory.learner_type_confidence or "high",
        primary_goal=memory.primary_goal,
        primary_deadline_event=memory.primary_deadline_event,
        primary_deadline_date=memory.primary_deadline_date,
        grade_level=memory.grade_level,
        board_or_curriculum=memory.board_or_curriculum,
        subjects=list(memory.subjects or []),
        syllabus_stage=dict(memory.syllabus_stage or {}),
        strong_topics=list(memory.strong_topics or []),
        weak_topics=list(memory.weak_topics or []),
        concept_gaps=list(memory.concept_gaps or []),
        misconceptions=list(memory.misconceptions or []),
        explanation_style=memory.explanation_style,
        session_profile=memory.session_profile,
        language_profile=memory.language_profile,
        peak_hours=memory.peak_hours,
        exam_name=memory.exam_name,
        exam_date=memory.exam_date,
        marks_target=memory.marks_target,
        mock_scores=list(memory.mock_scores or []),
        progress_trend=dict(memory.progress_trend or {}),
        competitive_exam_context=dict(memory.competitive_exam_context or {}),
        higher_education_context=dict(memory.higher_education_context or {}),
        professional_cert_context=dict(memory.professional_cert_context or {}),
        skill_learner_context=dict(memory.skill_learner_context or {}),
        medical_context=dict(memory.medical_context or {}),
        forgetting_stages=dict(memory.forgetting_stages or {}),
        improvement_velocity=dict(memory.improvement_velocity or {}),
        streak=memory.streak,
        last_topic_studied=memory.last_topic_studied,
        schema_version=int(memory.schema_version or 1),
        last_extraction_at=memory.last_extraction_at,
        extraction_source_job_ids=[str(item) for item in memory.extraction_source_job_ids or []],
        created_at=memory.created_at,
        updated_at=memory.updated_at,
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
    if idempotency_key:
        cached_job = await memory_service.get_idempotent_memory_add(
            tenant_id=tenant_id,
            idempotency_key=idempotency_key,
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

    gate_messages = [message.model_dump() for message in payload.messages]
    if payload.source is not None:
        # Registered backend events use service + event_id + payload_hash as
        # their idempotency boundary. Conversational semantic deduplication
        # would otherwise discard legitimate updates with similar templates.
        gate_result = await quality_gate_service.check(
            gate_messages,
            tenant_id,
            payload.external_user_id,
            semantic_deduplication=False,
        )
    else:
        gate_result = await quality_gate_service.check(
            gate_messages,
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
        api_key_id=str(getattr(request.state, "api_key_id", "") or "") or None,
        source=payload.source.model_dump(mode="json") if payload.source else None,
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
    cache_service: Annotated[CacheService, Depends(get_cache_service)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
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
        time_filter_days=payload.time_filter_days,
        quota_mode=getattr(getattr(request.state, "quota_envelope", None), "mode", None)
        or getattr(request.state, "quota_mode", None),
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
            source_event_id=result.source_event_id,
            provenance=result.provenance,
        )
        for result in results
    ]
    context = context_builder.build(
        results,
        format=payload.format,
        max_tokens=payload.context_max_tokens,
    )
    system_prompt_addition = context.system_prompt_addition
    context_token_count = context.token_count
    domain_schema_name = await _tenant_domain_schema(session, tenant_id)
    domain_schema = get_domain_schema(domain_schema_name)
    if domain_schema is not None:
        domain_prompt, domain_token_count = await domain_schema.build_retrieve_context(
            session=session,
            cache_service=cache_service,
            proxy_user_id=str(proxy_user.id),
            tenant_id=tenant_id,
            query=payload.query,
            max_tokens=min(600, payload.context_max_tokens),
        )
        if domain_prompt:
            system_prompt_addition = (
                domain_prompt
                + ("\n\n" + system_prompt_addition if system_prompt_addition else "")
            )
            context_token_count += domain_token_count
    clarification_question = await _pop_next_clarification_question(
        session=session,
        proxy_user_id=str(proxy_user.id),
    )
    retrieval_id = None
    try:
        retrieval_event = await RetrievalFeedbackService(session=session).log_retrieval(
            tenant_id=tenant_id,
            proxy_user_id=str(proxy_user.id),
            external_user_id=payload.external_user_id,
            query=payload.query,
            categories=[str(category.value if hasattr(category, "value") else category) for category in payload.categories],
            agent_id=payload.agent_id,
            retrieved_memory_ids=[result.id for result in results],
            result_count=len(results),
            top_relevance_score=float(results[0].final_score) if results else None,
            included_in_prompt=bool(system_prompt_addition),
            cache_hit=bool(retriever_service.last_cache_hit),
            quota_mode=getattr(retriever_service, "last_quota_mode", None),
            is_degraded=bool(getattr(retriever_service, "last_is_degraded", False)),
            metadata={"request_id": get_request_id(request)},
        )
        retrieval_id = str(retrieval_event.id)
    except Exception as exc:
        logger.warning("retrieval feedback logging failed: %s", exc)

    return MemoryRetrieveResponse(
        retrieval_id=retrieval_id,
        data=data,
        cached=bool(retriever_service.last_cache_hit),
        system_prompt_addition=system_prompt_addition,
        context_token_count=context_token_count,
        clarification_question=clarification_question,
        quota_mode=getattr(retriever_service, "last_quota_mode", None),
        is_degraded=bool(getattr(retriever_service, "last_is_degraded", False)),
        is_passthrough=getattr(retriever_service, "last_quota_mode", None) == "passthrough",
        request_id=get_request_id(request),
        timestamp=utc_now(),
    )


@router.post("/retrieval-feedback", response_model=RetrievalFeedbackResponse)
async def record_retrieval_feedback(
    request: Request,
    payload: RetrievalFeedbackRequest,
    memory_service: Annotated[MemoryService, Depends(get_memory_service)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
    tenant_id: str = Depends(get_authenticated_tenant_id),
) -> RetrievalFeedbackResponse:
    """Record whether a retrieved memory helped, was ignored, or was corrected by the user."""
    if payload.outcome == "user_corrected" and not payload.correction:
        raise APIError(
            status_code=422,
            code="REQ_422",
            error="correction_required_for_user_corrected",
        )

    feedback = await RetrievalFeedbackService(session=session).record_feedback(
        tenant_id=tenant_id,
        retrieval_id=str(payload.retrieval_id),
        outcome=payload.outcome,
        used_memory_ids=[str(memory_id) for memory_id in payload.used_memory_ids],
        correction=payload.correction,
        agent_confidence=payload.agent_confidence,
        metadata=payload.metadata,
        memory_service=memory_service,
        api_key_id=str(getattr(request.state, "api_key_id", "") or "") or None,
    )
    return RetrievalFeedbackResponse(
        data=RetrievalFeedbackData(
            feedback_id=str(feedback.id),
            retrieval_id=str(feedback.retrieval_event_id),
            outcome=feedback.outcome,
            correction_job_id=str(feedback.correction_job_id) if feedback.correction_job_id else None,
        ),
        request_id=get_request_id(request),
        timestamp=utc_now(),
    )


@router.get("/edtech-profile", response_model=EdTechProfileResponse)
async def get_edtech_profile(
    request: Request,
    proxy_user_service: Annotated[ProxyUserService, Depends(get_proxy_user_service)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
    tenant_id: str = Depends(get_authenticated_tenant_id),
    external_user_id: str = Query(min_length=1),
) -> EdTechProfileResponse:
    proxy_user = await proxy_user_service.resolve(
        tenant_id=tenant_id,
        external_user_id=external_user_id,
    )
    memory = (
        await session.execute(
            select(EdTechMemory).where(
                EdTechMemory.proxy_user_id == proxy_user.id,
                EdTechMemory.tenant_id == uuid.UUID(str(tenant_id)),
            )
        )
    ).scalar_one_or_none()
    return EdTechProfileResponse(
        data=_edtech_memory_to_view(memory) if memory is not None else None,
        request_id=get_request_id(request),
        timestamp=utc_now(),
    )


async def _pop_next_clarification_question(
    *,
    session: AsyncSession,
    proxy_user_id: str,
) -> str | None:
    result = await session.execute(
        select(ClarificationQueue)
        .where(
            ClarificationQueue.proxy_user_id == proxy_user_id,
            ClarificationQueue.status == ClarificationQueueStatus.pending,
            ClarificationQueue.trigger_on == "next_session",
            ClarificationQueue.expires_at > utc_now(),
        )
        .order_by(ClarificationQueue.created_at.asc())
        .limit(1)
    )
    scalar_one_or_none = getattr(result, "scalar_one_or_none", None)
    if not callable(scalar_one_or_none):
        return None
    clarification = scalar_one_or_none()
    if clarification is None:
        return None

    clarification.status = ClarificationQueueStatus.triggered
    await session.commit()
    return f"Quick check: {clarification.question_context}. Has anything changed?"


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
            pending_candidates_buffered=int(job.get("pending_candidates_buffered", 0) or 0),
            pending_candidates_promoted=int(job.get("pending_candidates_promoted", 0) or 0),
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
