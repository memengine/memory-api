from __future__ import annotations

import asyncio
import hashlib
import json
import os
import time
import traceback
import uuid
from datetime import UTC
from datetime import datetime
from difflib import SequenceMatcher
from datetime import timedelta
from typing import Any
import logging

import redis
import sentry_sdk
from celery import shared_task
from celery.signals import task_postrun
from celery.signals import worker_process_shutdown
from sqlalchemy import func
from sqlalchemy import select
from sqlalchemy.orm import Session
from sqlalchemy.orm import sessionmaker

from api.db.cache import HOT_MEMORIES_SUFFIX
from api.db.database import build_sync_session_factory
from api.db.models import Conversation
from api.db.models import ConversationProcessingStatus
from api.db.models import DeadLetterJob
from api.db.models import ExtractionJob
from api.infra.protected_storage import encrypt_json_for_dual_write
from api.db.models import ExtractionJobStatus
from api.db.models import Memory
from api.db.models import MemorySourceEvent
from api.db.models import PendingExtractionCandidate
from api.db.models import ProxyUser
from api.db.models import User
from api.db.vector_store import QdrantService
from api.schemas.extraction_schemas import PendingExtractedMemory
from api.schemas.memory_schemas import ExtractedMemory
from api.infra.circuit_breaker_registry import CircuitBreakerRegistry
from api.infra.fallbacks import on_redis_open
from api.services.conflict_resolver import ConflictResolver
from api.services.domain_schemas.registry import get_domain_schema
from api.services.embedding_service import EmbeddingService
from api.services.extraction_service import ExtractionService
from api.services.importance_scorer import ImportanceScorer
from api.services.provenance_service import build_provenance_snapshot
from api.tasks.queue_router import release_extraction_slot_sync
from api.settings import get_settings


EXTRACTION_TASK_NAME = "api.tasks.extraction_tasks.process_extraction_job"
JOB_TTL_SECONDS = 3600
JOB_STALE_TIMEOUT = timedelta(minutes=10)
LOGGER = logging.getLogger("memoryos.extraction_jobs")
_EXTRACTION_SESSION_FACTORY: sessionmaker[Session] | None = None
_EXTRACTION_SESSION_FACTORY_PID: int | None = None


class ExtractionPipelineError(RuntimeError):
    """Safe extraction failure context for logs and durable job diagnostics."""

    def __init__(self, *, stage: str, cause: Exception) -> None:
        self.stage = stage
        self.cause = cause
        self.cause_type = type(cause).__name__
        super().__init__(f"extraction_pipeline_failed stage={stage} cause={self.cause_type}")


def _job_status_key(job_id: str) -> str:
    return f"job:{job_id}:status"


def _hot_memories_key(proxy_user_id: str) -> str:
    return f"user:{proxy_user_id}:{HOT_MEMORIES_SUFFIX}"


def _redis_client() -> redis.Redis:
    return redis.Redis.from_url(
        os.getenv("REDIS_URL") or get_settings().redis_url or (_raise_missing_redis_url()),
        encoding="utf-8",
        decode_responses=True,
    )


def _wait_for_development_crash_barrier(*, job_id: str, job_payload: dict[str, Any]) -> bool:
    metadata = dict(job_payload.get("metadata") or {})
    if metadata.get("_internal_celery_crash_barrier") is not True:
        return False
    if get_settings().app_env.strip().lower() in {"production", "prod"}:
        LOGGER.warning("development_crash_barrier_disabled_in_production job_id=%s", job_id)
        return False

    client = _redis_client()
    barrier_key = f"internal-benchmark:celery-crash-barrier:{job_id}"
    release_key = f"{barrier_key}:release"
    if not client.set(barrier_key, "armed", nx=True, ex=300):
        LOGGER.warning("development_crash_barrier_already_consumed job_id=%s", job_id)
        return False

    LOGGER.warning("development_crash_barrier_armed job_id=%s", job_id)
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        if client.get(release_key):
            client.delete(barrier_key, release_key)
            return True
        time.sleep(0.05)
    client.delete(barrier_key, release_key)
    return True


def _raise_missing_redis_url() -> str:
    raise RuntimeError("REDIS_URL is required.")


def _set_job_status(job_id: str, payload: dict[str, Any]) -> None:
    client = _redis_client()
    breaker = CircuitBreakerRegistry.get_instance().redis_cb
    breaker.call_sync(
        client.set,
        _job_status_key(job_id),
        json.dumps(payload, default=str),
        ex=JOB_TTL_SECONDS,
        fallback=lambda: on_redis_open(None),
    )


def _invalidate_proxy_user_cache(proxy_user_id: str) -> None:
    client = _redis_client()
    breaker = CircuitBreakerRegistry.get_instance().redis_cb
    breaker.call_sync(
        client.delete,
        _hot_memories_key(proxy_user_id),
        fallback=lambda: on_redis_open(None),
    )


def classify_error(exc: Exception | str) -> str:
    root_cause = exc.cause if isinstance(exc, ExtractionPipelineError) else exc
    message = str(root_cause or "")
    normalized = message.lower()
    if "503" in message or "Service Unavailable" in message:
        return "llm_provider_unavailable_503"
    if "429" in message or "rate limit" in normalized or "quota" in normalized:
        return "llm_rate_limited_429"
    if "401" in message or "403" in message or "invalid api key" in normalized:
        return "llm_auth_failed"
    if "timeout" in normalized or isinstance(exc, (TimeoutError, asyncio.TimeoutError)):
        return "timeout"
    if "connection" in normalized:
        return "connection_error"
    if "json" in normalized or isinstance(root_cause, json.JSONDecodeError):
        return "llm_invalid_response"
    if "extraction_spec" in normalized:
        return "missing_extraction_spec"
    return "unknown_error"


def _safe_failure_detail(exc: Exception) -> str:
    """Return durable diagnostic context without persisting tracebacks or customer data."""
    if isinstance(exc, ExtractionPipelineError):
        return f"extraction_pipeline_failed stage={exc.stage} cause={exc.cause_type}"
    _error_type, sanitized_error = _sanitize_job_error(exc)
    return sanitized_error


def _sanitize_job_error(exc: Exception | str) -> tuple[str, str]:
    if isinstance(exc, Exception):
        error_type = type(exc).__name__
        message = str(exc)
    else:
        error_type = "Error"
        message = str(exc)
    normalized = message.strip().lower()
    if "proxy user" in normalized and "not found" in normalized:
        return error_type, "proxy_user_not_found"
    if "dispatch" in normalized and "failed" in normalized:
        return error_type, "dispatch_failed"
    if "timed out" in normalized or "timeout" in normalized:
        return error_type, "dependency_timeout"
    return error_type, error_type.lower()


def _capture_error_detail() -> str:
    error_detail = traceback.format_exc()
    if len(error_detail) > 3000:
        error_detail = error_detail[-2000:]
    return error_detail


def _normalize_stored_error(error: str) -> str:
    if "Traceback (most recent call last):" in error:
        return error
    _error_type, sanitized_error = _sanitize_job_error(error)
    return sanitized_error


def _upsert_dead_letter_job(session: Session, *, job: ExtractionJob, error: str, error_type: str | None) -> None:
    dead_letter = session.query(DeadLetterJob).filter(DeadLetterJob.job_id == job.id).one_or_none()
    if dead_letter is None:
        dead_letter = DeadLetterJob(
            job_id=job.id,
            tenant_id=job.tenant_id,
            proxy_user_id=job.proxy_user_id,
        )
    dead_letter.celery_task_id = job.celery_task_id
    dead_letter.attempts = int(job.attempts or 0)
    dead_letter.payload = dict(job.payload or {})
    dead_letter.error = error
    dead_letter.error_type = error_type
    session.add(dead_letter)


def _set_db_job_processing(*, job_id: str, celery_task_id: str | None) -> int | None:
    session_factory = build_extraction_session_factory()
    session = session_factory()
    try:
        job = session.execute(
            select(ExtractionJob)
            .where(ExtractionJob.id == uuid.UUID(job_id))
            .with_for_update()
        ).scalar_one_or_none()
        if job is None:
            return None
        stored_task_id = str(job.celery_task_id or "").strip()
        incoming_task_id = str(celery_task_id or "").strip()
        if job.status not in {ExtractionJobStatus.queued, ExtractionJobStatus.failed}:
            session.rollback()
            return None
        if stored_task_id and stored_task_id != incoming_task_id:
            session.rollback()
            return None
        job.status = ExtractionJobStatus.processing
        job.celery_task_id = celery_task_id
        started_at = datetime.now(UTC)
        job.processing_started_at = started_at
        job.started_at = started_at
        job.stale_after = started_at + JOB_STALE_TIMEOUT
        job.completed_at = None
        job.updated_at = datetime.now(UTC)
        job.error = None
        job.error_type = None
        session.add(job)
        session.commit()
        return int(job.attempts or 0)
    finally:
        session.close()


def _set_db_job_completed(*, job_id: str, payload: dict[str, Any]) -> None:
    session_factory = build_extraction_session_factory()
    session = session_factory()
    try:
        job = session.get(ExtractionJob, uuid.UUID(job_id))
        if job is None:
            return
        job.status = ExtractionJobStatus.completed
        job.memories_created = int(payload.get("memories_created", 0) or 0)
        job.result = payload
        job.result_envelope = encrypt_json_for_dual_write(
            tenant_id=str(job.tenant_id),
            record_type="extraction-job-result",
            record_id=str(job.id),
            value=payload,
        )
        job.completed_at = datetime.now(UTC)
        job.stale_after = None
        job.updated_at = datetime.now(UTC)
        job.error = None
        job.error_type = None
        session.add(job)
        session.commit()
    finally:
        session.close()


def _set_db_job_failure(*, job_id: str, error: str, error_type: str | None = None) -> tuple[ExtractionJobStatus, int, int]:
    session_factory = build_extraction_session_factory()
    session = session_factory()
    try:
        job = session.get(ExtractionJob, uuid.UUID(job_id))
        if job is None:
            return ExtractionJobStatus.failed, 1, 3
        max_attempts = int(job.max_attempts or 3)
        next_attempts = int(job.attempts or 0) + 1
        next_status = ExtractionJobStatus.dead if next_attempts >= max_attempts else ExtractionJobStatus.failed
        stored_error_type = error_type or classify_error(error)
        stored_error = _normalize_stored_error(error)
        job.status = next_status
        job.attempts = next_attempts
        job.error = stored_error
        job.error_type = stored_error_type
        job.stale_after = None
        job.celery_task_id = None
        job.updated_at = datetime.now(UTC)
        if next_status == ExtractionJobStatus.dead:
            finished_at = datetime.now(UTC)
            job.completed_at = finished_at
            job.dead_lettered_at = finished_at
            _upsert_dead_letter_job(session, job=job, error=stored_error, error_type=stored_error_type)
            sentry_sdk.capture_message(
                f"Extraction job dead-lettered job_id={job_id} tenant_id={job.tenant_id} error_type={stored_error_type}",
                level="error",
            )
        session.add(job)
        session.commit()
        return next_status, next_attempts, max_attempts
    finally:
        session.close()


def _force_dead_letter_job(*, job_id: str, error: str, error_type: str | None = None) -> ExtractionJobStatus:
    session_factory = build_extraction_session_factory()
    session = session_factory()
    try:
        job = session.get(ExtractionJob, uuid.UUID(job_id))
        if job is None:
            return ExtractionJobStatus.dead
        stored_error_type = error_type or classify_error(error)
        stored_error = _normalize_stored_error(error)
        job.status = ExtractionJobStatus.dead
        job.error = stored_error
        job.error_type = stored_error_type
        job.stale_after = None
        job.celery_task_id = None
        finished_at = datetime.now(UTC)
        job.completed_at = finished_at
        job.dead_lettered_at = finished_at
        job.updated_at = finished_at
        _upsert_dead_letter_job(session, job=job, error=stored_error, error_type=stored_error_type)
        session.add(job)
        session.commit()
        sentry_sdk.capture_message(
            f"Extraction job dead-lettered job_id={job_id} tenant_id={job.tenant_id} error_type={stored_error_type}",
            level="error",
        )
        return ExtractionJobStatus.dead
    finally:
        session.close()


def build_extraction_session_factory() -> sessionmaker[Session]:
    global _EXTRACTION_SESSION_FACTORY
    global _EXTRACTION_SESSION_FACTORY_PID

    process_id = os.getpid()
    if (
        _EXTRACTION_SESSION_FACTORY is None
        or _EXTRACTION_SESSION_FACTORY_PID != process_id
    ):
        dispose_extraction_session_factory()
        _EXTRACTION_SESSION_FACTORY = build_sync_session_factory()
        _EXTRACTION_SESSION_FACTORY_PID = process_id
    return _EXTRACTION_SESSION_FACTORY


@worker_process_shutdown.connect
def dispose_extraction_session_factory(**_extra: Any) -> None:
    """Dispose the process-local extraction engine when a Celery child exits."""
    global _EXTRACTION_SESSION_FACTORY
    global _EXTRACTION_SESSION_FACTORY_PID

    factory = _EXTRACTION_SESSION_FACTORY
    _EXTRACTION_SESSION_FACTORY = None
    _EXTRACTION_SESSION_FACTORY_PID = None
    if factory is None:
        return
    engine = factory.kw.get("bind")
    if engine is not None:
        engine.dispose()


def _ensure_proxy_backing_user(session: Session, proxy_user_id: str) -> User:
    external_id = f"proxy::{proxy_user_id}"
    user = session.execute(select(User).where(User.external_id == external_id)).scalar_one_or_none()
    if user is not None:
        return user

    user = User(
        id=uuid.uuid4(),
        external_id=external_id,
        email=f"proxy+{proxy_user_id}@memoryos.internal",
        settings={},
        memory_count=0,
        is_active=True,
    )
    session.add(user)
    session.flush()
    return user


def _parse_optional_uuid(value: str | None) -> uuid.UUID | None:
    if not value:
        return None
    try:
        return uuid.UUID(str(value))
    except (TypeError, ValueError):
        return None


def _create_source_conversation(
    session: Session,
    *,
    user_id: uuid.UUID,
    agent_id: str | None,
    message_count: int,
) -> Conversation:
    conversation = Conversation(
        id=uuid.uuid4(),
        user_id=user_id,
        agent_id=_parse_optional_uuid(agent_id),
        message_count=message_count,
        processing_status=ConversationProcessingStatus.processing,
    )
    session.add(conversation)
    session.flush()
    return conversation


def _refresh_proxy_user_memory_count(session: Session, proxy_user_id: uuid.UUID) -> None:
    proxy_user = session.get(ProxyUser, proxy_user_id)
    if proxy_user is None:
        return

    memory_count = session.execute(
        select(func.count(Memory.id)).where(Memory.proxy_user_id == proxy_user_id)
    ).scalar_one()
    proxy_user.memory_count = int(memory_count or 0)
    proxy_user.last_active_at = datetime.now(UTC)
    session.add(proxy_user)


def _serialize_stored_memories(stored_memories: list[Any]) -> list[dict[str, Any]]:
    serialized: list[dict[str, Any]] = []
    for memory in stored_memories:
        serialized.append(
            {
                "id": memory.id,
                "user_id": memory.user_id,
                "proxy_user_id": memory.proxy_user_id,
                "content": memory.content,
                "category": memory.category,
                "importance_score": memory.importance_score,
                "confidence_score": memory.confidence_score,
                "previous_version_id": memory.previous_version_id,
                "resolution": memory.resolution,
            }
        )
    return serialized


def _normalize_candidate_text(value: str) -> str:
    return " ".join(value.strip().lower().rstrip(".?!").split())


def _candidate_fingerprint(candidate: PendingExtractedMemory) -> str:
    canonical = repr(
        {
            "category": str(candidate.category).lower(),
            "content": _normalize_candidate_text(candidate.content),
        }
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


_NEGATION_TOKENS = {
    "avoid",
    "dislike",
    "dislikes",
    "don't",
    "dont",
    "doesn't",
    "doesnt",
    "hate",
    "hates",
    "never",
    "no",
    "not",
    "prefer not",
    "stopped",
    "without",
}
_PENDING_SIMILARITY_THRESHOLD = 0.82
_PENDING_PROMOTION_REINFORCEMENT_COUNT = 2


def _candidate_similarity(left: str, right: str) -> float:
    normalized_left = _normalize_candidate_text(left)
    normalized_right = _normalize_candidate_text(right)
    if not normalized_left or not normalized_right:
        return 0.0
    if normalized_left == normalized_right:
        return 1.0
    left_tokens = set(normalized_left.split())
    right_tokens = set(normalized_right.split())
    token_overlap = len(left_tokens & right_tokens) / max(1, len(left_tokens | right_tokens))
    sequence_ratio = SequenceMatcher(None, normalized_left, normalized_right).ratio()
    return max(token_overlap, sequence_ratio)


def _candidate_polarity(value: str) -> str:
    normalized = f" {_normalize_candidate_text(value)} "
    for token in _NEGATION_TOKENS:
        if f" {token} " in normalized:
            return "negative"
    return "positive"


def _can_reinforce_candidate(existing: Any, candidate: PendingExtractedMemory) -> bool:
    return (
        _candidate_similarity(str(getattr(existing, "content", "") or ""), candidate.content)
        >= _PENDING_SIMILARITY_THRESHOLD
        and _candidate_polarity(str(getattr(existing, "content", "") or ""))
        == _candidate_polarity(candidate.content)
    )


def _memory_category_value(value: Any) -> str:
    return str(getattr(value, "value", value)).lower()


def _find_matching_pending_candidate(
    session: Session,
    *,
    tenant_id: uuid.UUID,
    proxy_user_id: uuid.UUID,
    candidate: PendingExtractedMemory,
    fingerprint: str,
) -> tuple[PendingExtractionCandidate | None, bool]:
    exact = session.execute(
        select(PendingExtractionCandidate).where(
            PendingExtractionCandidate.tenant_id == tenant_id,
            PendingExtractionCandidate.proxy_user_id == proxy_user_id,
            PendingExtractionCandidate.candidate_fingerprint == fingerprint,
        )
    ).scalar_one_or_none()
    if exact is not None:
        if _can_reinforce_candidate(exact, candidate):
            return exact, False
        return None, True

    pending_for_category = session.execute(
        select(PendingExtractionCandidate).where(
            PendingExtractionCandidate.tenant_id == tenant_id,
            PendingExtractionCandidate.proxy_user_id == proxy_user_id,
            PendingExtractionCandidate.category == candidate.category,
            PendingExtractionCandidate.status == "pending",
        )
    ).scalars().all()
    for existing in pending_for_category:
        if _can_reinforce_candidate(existing, candidate):
            return existing, False
    return None, False


def _promoted_memory_from_candidate(candidate: PendingExtractionCandidate) -> ExtractedMemory:
    return ExtractedMemory(
        content=candidate.content,
        category=_memory_category_value(candidate.category),  # type: ignore[arg-type]
        importance_score=max(1.0, min(10.0, float(candidate.importance_score or 1.0))),
        confidence=max(0.0, min(1.0, float(candidate.confidence_score or 0.0))),
        expiry="permanent",
        reasoning=str(candidate.reasoning or "Promoted after repeated borderline extraction."),
    )


def _should_promote_pending_candidate(candidate: PendingExtractionCandidate, *, store_threshold: float = 0.65) -> bool:
    return int(candidate.reinforcement_count or 0) >= _PENDING_PROMOTION_REINFORCEMENT_COUNT or float(
        candidate.confidence_score or 0.0
    ) >= store_threshold


def _persist_pending_extraction_candidates(
    session: Session,
    *,
    candidates: list[PendingExtractedMemory],
    tenant_id: str,
    proxy_user_id: str,
    extraction_job_id: str | None,
    source_event_id: str | None,
) -> tuple[int, list[ExtractedMemory]]:
    if not candidates:
        return 0, []

    tenant_uuid = uuid.UUID(str(tenant_id))
    proxy_user_uuid = uuid.UUID(str(proxy_user_id))
    job_uuid = uuid.UUID(str(extraction_job_id)) if extraction_job_id else None
    event_uuid = uuid.UUID(str(source_event_id)) if source_event_id else None
    now = datetime.now(UTC)
    buffered = 0
    promoted: list[ExtractedMemory] = []

    for candidate in candidates:
        fingerprint = _candidate_fingerprint(candidate)
        existing, conflict_candidate = _find_matching_pending_candidate(
            session,
            tenant_id=tenant_uuid,
            proxy_user_id=proxy_user_uuid,
            candidate=candidate,
            fingerprint=fingerprint,
        )

        if existing is None:
            if conflict_candidate:
                conflict_fingerprint_base = f"{fingerprint}:conflict:{_normalize_candidate_text(candidate.content)}"
                fingerprint = hashlib.sha256(conflict_fingerprint_base.encode("utf-8")).hexdigest()
            existing = PendingExtractionCandidate(
                tenant_id=tenant_uuid,
                proxy_user_id=proxy_user_uuid,
                extraction_job_id=job_uuid,
                source_event_id=event_uuid,
                content=candidate.content,
                category=candidate.category,
                importance_score=candidate.importance_score,
                confidence_score=candidate.confidence,
                reasoning=candidate.reasoning,
                candidate_reason=candidate.candidate_reason,
                candidate_fingerprint=fingerprint,
                status="pending",
                reinforcement_count=1,
                metadata_json={"conflict_candidate": True} if conflict_candidate else {},
                last_seen_at=now,
            )
        else:
            existing.extraction_job_id = job_uuid or existing.extraction_job_id
            existing.source_event_id = event_uuid or existing.source_event_id
            existing.content = candidate.content
            existing.importance_score = max(float(existing.importance_score or 0.0), candidate.importance_score)
            existing.confidence_score = max(float(existing.confidence_score or 0.0), candidate.confidence)
            existing.reasoning = candidate.reasoning
            existing.candidate_reason = candidate.candidate_reason
            existing.reinforcement_count = int(existing.reinforcement_count or 0) + 1
            existing.last_seen_at = now
            existing.updated_at = now
            if existing.status != "pending":
                existing.status = "pending"

        if _should_promote_pending_candidate(existing):
            existing.status = "promoted"
            existing.updated_at = now
            metadata = dict(existing.metadata_json or {})
            metadata["promoted_at"] = now.isoformat()
            metadata["promotion_reason"] = "reinforced_borderline_candidate"
            existing.metadata_json = metadata
            promoted.append(_promoted_memory_from_candidate(existing))

        session.add(existing)
        buffered += 1

    return buffered, promoted

def _load_existing_memories_for_context(session: Session, proxy_user_id: str) -> list[Memory]:
    try:
        proxy_user_uuid = uuid.UUID(str(proxy_user_id))
    except (TypeError, ValueError):
        return []
    try:
        result = session.execute(
            select(Memory)
            .where(
                Memory.proxy_user_id == proxy_user_uuid,
                Memory.is_archived.is_(False),
            )
            .order_by(Memory.importance_score.desc())
            .limit(50)
        )
        return list(result.scalars().all())
    except Exception:
        return []


def _tenant_domain_schema(session: Session, tenant_id: str) -> str | None:
    try:
        from api.db.models import Tenant

        tenant = session.get(Tenant, uuid.UUID(str(tenant_id)))
    except Exception:
        return None
    if tenant is None:
        return None
    metadata = getattr(tenant, "metadata_json", None) or {}
    return metadata.get("domain_schema") or metadata.get("memory_domain")


def _run_domain_schema_overlay(
    session: Session,
    *,
    messages: list[dict[str, Any]],
    proxy_user_id: str,
    tenant_id: str,
    job_id: str,
    agent_id: str | None,
    client: Any | None,
) -> dict[str, Any] | None:
    domain_schema = _tenant_domain_schema(session, tenant_id)
    if domain_schema is None:
        return None
    schema = get_domain_schema(domain_schema)
    if schema is None:
        return None
    try:
        result = schema.extract_overlay_sync(
            session=session,
            messages=messages,
            proxy_user_id=proxy_user_id,
            tenant_id=tenant_id,
            job_id=job_id,
            agent_id=agent_id,
            client=client,
        )
        session.commit()
        return result
    except Exception as exc:
        session.rollback()
        LOGGER.warning(
            "domain_schema_extraction_failed",
            extra={
                "event": "domain_schema_extraction_failed",
                "domain_schema": domain_schema,
                "tenant_id": tenant_id,
                "proxy_user_id": proxy_user_id,
                "job_id": job_id,
                "error": str(exc),
            },
        )
        return {"domain_schema_error": str(exc), "domain_schema": domain_schema}


def _extract_memories_for_pipeline(
    extractor: Any,
    *,
    messages: list[dict[str, Any]],
    proxy_user_id: str,
    tenant_id: str,
    job_id: str | None,
    existing_memories: list[Memory],
    source_context: dict[str, Any] | None = None,
) -> tuple[list[Any], dict[str, Any], bool]:
    """Run either the new spec-driven extractor or a legacy test double.

    Returns: extracted memories, metadata, whether the pipeline should apply the
    legacy ImportanceScorer pass.
    """
    if isinstance(extractor, ExtractionService):
        result = extractor.extract_sync(
            messages=messages,
            proxy_user_id=proxy_user_id,
            tenant_id=tenant_id,
            job_id=job_id,
            existing_memories=existing_memories,
            source_context=source_context,
        )
        return (
            list(result.memories_to_store),
            {
                "memories_filtered": result.memories_filtered,
                "pending_candidates_count": result.pending_candidates_count,
                "pending_candidates": list(result.pending_candidates),
                "nothing_to_extract": result.nothing_to_extract,
                "tokens_used": result.tokens_used,
                "provider_used": result.provider_used,
                "extraction_metadata": dict(result.extraction_metadata or {}),
            },
            False,
        )

    extracted = extractor.extract(
        messages=messages,
        user_id=proxy_user_id,
    )
    return list(extracted), {}, True


def run_extraction_pipeline(
    job_payload: dict[str, Any],
    *,
    session_factory: sessionmaker[Session] | None = None,
    extractor: ExtractionService | None = None,
    scorer: ImportanceScorer | None = None,
    qdrant_service: QdrantService | None = None,
    conflict_resolver: ConflictResolver | None = None,
    client: Any | None = None,
) -> dict[str, Any]:
    tenant_id = str(job_payload.get("tenant_id") or "").strip()
    proxy_user_id = str(job_payload.get("proxy_user_id") or "").strip()
    agent_id = job_payload.get("agent_id")
    source_event_id = job_payload.get("source_event_id")
    messages = list(job_payload.get("messages", []))

    if not tenant_id or not proxy_user_id:
        raise ValueError("Extraction job requires tenant_id and proxy_user_id.")

    session_factory = session_factory or build_extraction_session_factory()
    extractor = extractor or ExtractionService(client=client)
    scorer = scorer or ImportanceScorer()
    qdrant_service = qdrant_service or QdrantService()

    session = session_factory()
    conversation: Conversation | None = None
    stage = "load_proxy_user"

    try:
        proxy_user = session.get(ProxyUser, uuid.UUID(proxy_user_id))
        if proxy_user is None:
            raise ValueError(f"Proxy user {proxy_user_id} not found.")

        stage = "create_conversation"
        backing_user = _ensure_proxy_backing_user(session, proxy_user_id)
        conversation = _create_source_conversation(
            session,
            user_id=backing_user.id,
            agent_id=str(agent_id) if agent_id else None,
            message_count=len(messages),
        )
        session.commit()

        stage = "load_context"
        existing_memories = _load_existing_memories_for_context(session, proxy_user_id)
        domain_schema_name = _tenant_domain_schema(session, tenant_id)
        source_event = (
            session.get(MemorySourceEvent, uuid.UUID(str(source_event_id)))
            if source_event_id
            else None
        )
        source_payload = dict(job_payload.get("source") or {})
        source_context = (
            build_provenance_snapshot(source_event)
            if source_event is not None and source_payload.get("explicit") is True
            else None
        )
        stage = "extract_memories"
        try:
            extracted_memories, extraction_meta, should_apply_scorer = _extract_memories_for_pipeline(
                extractor,
                messages=messages,
                proxy_user_id=proxy_user_id,
                tenant_id=tenant_id,
                job_id=str(job_payload.get("job_id") or ""),
                existing_memories=existing_memories,
                source_context=source_context,
            )
        except Exception as exc:
            if not domain_schema_name:
                raise
            LOGGER.warning(
                "general_extraction_failed_domain_overlay_continuing",
                extra={
                    "event": "general_extraction_failed_domain_overlay_continuing",
                    "domain_schema": domain_schema_name,
                    "tenant_id": tenant_id,
                    "proxy_user_id": proxy_user_id,
                    "job_id": str(job_payload.get("job_id") or ""),
                    "error": str(exc),
                },
            )
            extracted_memories = []
            extraction_meta = {
                "memories_filtered": 0,
                "nothing_to_extract": True,
                "tokens_used": 0,
                "provider_used": None,
                "general_extraction_error": str(exc),
            }
            should_apply_scorer = False
        stage = "persist_pending_candidates"
        pending_candidates_buffered, promoted_pending_memories = _persist_pending_extraction_candidates(
            session,
            candidates=list(extraction_meta.get("pending_candidates", []) or []),
            tenant_id=tenant_id,
            proxy_user_id=proxy_user_id,
            extraction_job_id=str(job_payload.get("job_id") or "") or None,
            source_event_id=str(source_event.id) if source_event is not None else None,
        )
        extraction_meta["pending_candidates_buffered"] = pending_candidates_buffered

        if should_apply_scorer:
            for memory in extracted_memories:
                memory.importance_score = scorer.score(
                    memory,
                    {"similar_access_count": int(proxy_user.memory_count or 0)},
                )

        if source_event is not None:
            source_event.processing_metadata = {
                **dict(source_event.processing_metadata or {}),
                "provider_used": extraction_meta.get("provider_used"),
                "domain_schema": domain_schema_name or "general",
                "extraction_metadata": extraction_meta.get("extraction_metadata") or {},
            }

        stage = "store_memories"
        embedding_service = EmbeddingService(sync_session=session, gemini_client=client)
        resolver = conflict_resolver or ConflictResolver(
            session=session,
            qdrant_service=qdrant_service,
            embedder=embedding_service.embed_sync,
            client=client,
            default_source_conversation_id=conversation.id,
            default_source_event_id=source_event.id if source_event is not None else None,
            provenance_snapshot=(
                build_provenance_snapshot(source_event) if source_event is not None else None
            ),
            domain_schema=domain_schema_name,
        )
        stored_memories = resolver.check_and_store(
            extracted_memories,
            user_id=str(backing_user.id),
            tenant_id=tenant_id,
            proxy_user_id=proxy_user_id,
            source_conversation_id=str(conversation.id),
            agent_id=str(agent_id) if agent_id else None,
            auto_commit=False,
        )

        conversation.processing_status = ConversationProcessingStatus.done
        session.add(conversation)
        if source_event is not None:
            source_event.processing_metadata = {
                **dict(source_event.processing_metadata or {}),
                "provider_used": extraction_meta.get("provider_used"),
                "domain_schema": domain_schema_name or "general",
                "extraction_metadata": extraction_meta.get("extraction_metadata") or {},
                "completed_at": datetime.now(UTC).isoformat(),
            }
            session.add(source_event)
        _refresh_proxy_user_memory_count(session, proxy_user.id)
        session.commit()

        stage = "run_domain_overlay"
        domain_schema_meta = _run_domain_schema_overlay(
            session,
            messages=messages,
            proxy_user_id=proxy_user_id,
            tenant_id=tenant_id,
            job_id=str(job_payload.get("job_id") or ""),
            agent_id=str(agent_id) if agent_id else None,
            client=client,
        ) or {}

        stage = "invalidate_cache"
        _invalidate_proxy_user_cache(proxy_user_id)

        conflicts_resolved = sum(
            1
            for memory in stored_memories
            if getattr(memory, "resolution", None) in {"UPDATE", "MERGE"}
        )
        return {
            **job_payload,
            "status": "processed",
            "memories_created": len(stored_memories),
            "stored_memories": _serialize_stored_memories(stored_memories),
            "memories_filtered": int(extraction_meta.get("memories_filtered", 0) or 0),
            "pending_candidates_buffered": int(extraction_meta.get("pending_candidates_buffered", 0) or 0),
            "pending_candidates_promoted": int(extraction_meta.get("pending_candidates_promoted", 0) or 0),
            "nothing_to_extract": bool(extraction_meta.get("nothing_to_extract", False)),
            "conflicts_resolved": conflicts_resolved,
            "cross_user_conflicts_flagged": int(getattr(resolver, "last_cross_user_conflicts_flagged", 0) or 0),
            "detection_strategies_used": list(getattr(resolver, "last_detection_strategies_used", []) or []),
            "conflict_types_found": list(getattr(resolver, "last_conflict_types_found", []) or []),
            "tokens_used": int(extraction_meta.get("tokens_used", 0) or 0),
            "provider_used": extraction_meta.get("provider_used"),
            "general_extraction_error": extraction_meta.get("general_extraction_error"),
            "extraction_metadata": extraction_meta.get("extraction_metadata") or {},
            **domain_schema_meta,
        }
    except Exception as exc:
        session.rollback()
        if conversation is not None:
            try:
                conversation.processing_status = ConversationProcessingStatus.failed
                session.add(conversation)
                session.commit()
            except Exception:
                session.rollback()
        raise ExtractionPipelineError(stage=stage, cause=exc) from exc
    finally:
        session.close()


@shared_task(bind=True, name=EXTRACTION_TASK_NAME, max_retries=3, default_retry_delay=2)
def process_extraction_job(self, job_payload: dict[str, Any]) -> dict[str, Any]:
    job_id = str(job_payload.get("job_id"))
    attempts = _set_db_job_processing(job_id=job_id, celery_task_id=self.request.id)
    if attempts is None:
        return {
            "job_id": job_id,
            "status": "duplicate_ignored",
            "memories_created": 0,
        }
    _wait_for_development_crash_barrier(job_id=job_id, job_payload=job_payload)
    processing_payload = {
        **job_payload,
        "status": "processing",
    }
    _set_job_status(job_id, processing_payload)

    try:
        completed_payload = run_extraction_pipeline(job_payload)
        _set_db_job_completed(job_id=job_id, payload=completed_payload)
        _set_job_status(job_id, completed_payload)
        return completed_payload
    except Exception as exc:
        error_type = classify_error(exc)
        error_detail = _safe_failure_detail(exc)
        LOGGER.error(
            "extraction_job_failed",
            extra={
                "event": "extraction_job_failed",
                "job_id": job_id,
                "tenant_id": str(job_payload.get("tenant_id") or ""),
                "queue_name": str(job_payload.get("queue_name") or ""),
                "error_type": error_type,
                "error_detail": error_detail,
            },
        )
        next_status, attempts, _max_attempts = _set_db_job_failure(
            job_id=job_id,
            error=error_detail,
            error_type=error_type,
        )
        failed_payload = {
            **job_payload,
            "status": next_status.value,
            "attempts": attempts,
            "error": error_detail,
            "error_type": error_type,
            "memories_created": 0,
        }
        _set_job_status(job_id, failed_payload)
        if next_status == ExtractionJobStatus.dead:
            return failed_payload

        retry_payload = {
            **job_payload,
            "_retain_queue_slot": True,
        }
        countdown = 60 * attempts
        try:
            process_extraction_job.apply_async(
                args=[retry_payload],
                queue=job_payload.get("queue_name"),
                countdown=countdown,
            )
        except Exception as retry_exc:
            retry_error_detail = _capture_error_detail()
            retry_error_type = classify_error(retry_exc)
            forced_status = _force_dead_letter_job(
                job_id=job_id,
                error=retry_error_detail,
                error_type=retry_error_type,
            )
            dead_payload = {
                **job_payload,
                "status": forced_status.value,
                "attempts": attempts,
                "error": retry_error_detail,
                "error_type": retry_error_type,
                "memories_created": 0,
            }
            _set_job_status(job_id, dead_payload)
            return dead_payload

        LOGGER.warning(
            "extraction_job_retry_scheduled job_id=%s tenant_id=%s attempts=%s error_type=%s countdown_seconds=%s",
            job_id,
            job_payload.get("tenant_id"),
            attempts,
            type(exc).__name__,
            countdown,
        )
        failed_payload["_retain_queue_slot"] = True
        return failed_payload


@task_postrun.connect
def release_queue_slot_after_extraction(
    sender=None,
    task_id=None,
    task=None,
    args=None,
    kwargs=None,
    retval=None,
    state=None,
    **_extra,
) -> None:
    if getattr(sender, "name", None) != EXTRACTION_TASK_NAME:
        return
    if state == "RETRY":
        return
    if not args:
        return
    payload = args[0] if isinstance(args[0], dict) else None
    if payload is None:
        return
    if isinstance(retval, dict) and retval.get("_retain_queue_slot"):
        return
    release_extraction_slot_sync(
        tenant_id=payload.get("tenant_id"),
        queue_name=payload.get("queue_name"),
        job_id=payload.get("job_id"),
    )
