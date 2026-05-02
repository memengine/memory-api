from __future__ import annotations

import asyncio
import json
import os
import traceback
import uuid
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from typing import Any
import logging

import redis
import sentry_sdk
from celery import shared_task
from celery.signals import task_postrun
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
from api.db.models import ExtractionJobStatus
from api.db.models import Memory
from api.db.models import ProxyUser
from api.db.models import User
from api.db.vector_store import QdrantService
from api.infra.circuit_breaker_registry import CircuitBreakerRegistry
from api.infra.fallbacks import on_redis_open
from api.services.conflict_resolver import ConflictResolver
from api.services.embedding_service import EmbeddingService
from api.services.extractor import ExtractionService
from api.services.importance_scorer import ImportanceScorer
from api.tasks.queue_router import release_extraction_slot_sync
from api.settings import get_settings


EXTRACTION_TASK_NAME = "api.tasks.extraction_tasks.process_extraction_job"
JOB_TTL_SECONDS = 3600
JOB_STALE_TIMEOUT = timedelta(minutes=10)
LOGGER = logging.getLogger("memoryos.extraction_jobs")


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
    message = str(exc or "")
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
    if "json" in normalized or isinstance(exc, (json.JSONDecodeError, ValueError)):
        return "llm_invalid_response"
    if "extraction_spec" in normalized:
        return "missing_extraction_spec"
    return "unknown_error"


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
        error_detail = (
            error_detail[:1500]
            + "\n\n... [truncated] ...\n\n"
            + error_detail[-1500:]
        )
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


def _set_db_job_processing(*, job_id: str, celery_task_id: str | None) -> int:
    session_factory = build_extraction_session_factory()
    session = session_factory()
    try:
        job = session.get(ExtractionJob, uuid.UUID(job_id))
        if job is None:
            return 0
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
    return build_sync_session_factory()


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
        agent_id=uuid.UUID(agent_id) if agent_id else None,
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
    messages = list(job_payload.get("messages", []))

    if not tenant_id or not proxy_user_id:
        raise ValueError("Extraction job requires tenant_id and proxy_user_id.")

    session_factory = session_factory or build_extraction_session_factory()
    extractor = extractor or ExtractionService(client=client)
    scorer = scorer or ImportanceScorer()
    qdrant_service = qdrant_service or QdrantService()

    session = session_factory()
    conversation: Conversation | None = None

    try:
        proxy_user = session.get(ProxyUser, uuid.UUID(proxy_user_id))
        if proxy_user is None:
            raise ValueError(f"Proxy user {proxy_user_id} not found.")

        backing_user = _ensure_proxy_backing_user(session, proxy_user_id)
        conversation = _create_source_conversation(
            session,
            user_id=backing_user.id,
            agent_id=str(agent_id) if agent_id else None,
            message_count=len(messages),
        )
        session.commit()

        extracted_memories = extractor.extract(
            messages=messages,
            user_id=proxy_user_id,
        )
        for memory in extracted_memories:
            memory.importance_score = scorer.score(
                memory,
                {"similar_access_count": int(proxy_user.memory_count or 0)},
            )

        embedding_service = EmbeddingService(sync_session=session, gemini_client=client)
        resolver = conflict_resolver or ConflictResolver(
            session=session,
            qdrant_service=qdrant_service,
            embedder=embedding_service.embed_sync,
            client=client,
            default_source_conversation_id=conversation.id,
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
        _refresh_proxy_user_memory_count(session, proxy_user.id)
        session.commit()

        _invalidate_proxy_user_cache(proxy_user_id)

        return {
            **job_payload,
            "status": "processed",
            "memories_created": len(stored_memories),
            "stored_memories": _serialize_stored_memories(stored_memories),
        }
    except Exception:
        session.rollback()
        if conversation is not None:
            try:
                conversation.processing_status = ConversationProcessingStatus.failed
                session.add(conversation)
                session.commit()
            except Exception:
                session.rollback()
        raise
    finally:
        session.close()


@shared_task(bind=True, name=EXTRACTION_TASK_NAME, max_retries=3, default_retry_delay=2)
def process_extraction_job(self, job_payload: dict[str, Any]) -> dict[str, Any]:
    job_id = str(job_payload.get("job_id"))
    _set_db_job_processing(job_id=job_id, celery_task_id=self.request.id)
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
        error_detail = _capture_error_detail()
        error_type = classify_error(exc)
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
