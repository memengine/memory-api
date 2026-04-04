from __future__ import annotations
import uuid
from datetime import UTC
from datetime import datetime

from fastapi import APIRouter
from fastapi import Depends
from fastapi import HTTPException
from fastapi import Request
from sqlalchemy import select
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from api.celery_app import celery_app
from api.db.cache import CacheService
from api.db.database import get_db_session
from api.db.models import DeadLetterJob
from api.db.models import ExtractionJob
from api.db.models import ExtractionJobStatus
from api.dependencies import get_cache_service
from api.routers.common import get_request_id
from api.services.embedding_service import EmbeddingService
from api.tasks.extraction_tasks import EXTRACTION_TASK_NAME
from api.tasks.backfill_tasks import run_backfill_proxy_user_ids
from api.tasks.queue_router import QueueRouter


router = APIRouter(prefix="/v1/internal", tags=["internal"])


def _result_all_rows(result) -> list[object]:
    if hasattr(result, "all"):
        return list(result.all())
    if hasattr(result, "scalars"):
        return list(result.scalars().all())
    return []


def _result_scalar_one_or_none(result):
    if hasattr(result, "scalar_one_or_none"):
        return result.scalar_one_or_none()
    if hasattr(result, "scalars"):
        rows = list(result.scalars().all())
        return rows[0] if rows else None
    return None


def _unpack_dead_letter_row(row) -> tuple[object | None, object]:
    if isinstance(row, tuple) and len(row) == 2:
        return row[0], row[1]
    if hasattr(row, "_mapping"):
        values = list(row._mapping.values())
        if len(values) >= 2:
            return values[0], values[1]
    if hasattr(row, "__getitem__"):
        try:
            return row[0], row[1]
        except Exception:
            pass
    return None, row


@router.get("/backfill-status")
async def backfill_status(
    request: Request,
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, object]:
    result = await session.execute(
        text(
            """
            SELECT
                id,
                task_name,
                status,
                total_rows,
                processed_rows,
                pct_complete,
                eta_seconds,
                started_at,
                completed_at,
                error
            FROM backfill_jobs
            ORDER BY started_at DESC
            """
        )
    )
    rows = []
    for row in result.mappings().all():
        rows.append(
            {
                "id": str(row["id"]),
                "task_name": row["task_name"],
                "status": row["status"],
                "total_rows": row["total_rows"],
                "processed_rows": row["processed_rows"],
                "pct_complete": float(row["pct_complete"] or 0),
                "eta_seconds": row["eta_seconds"],
                "started_at": None if row["started_at"] is None else row["started_at"].isoformat(),
                "completed_at": None if row["completed_at"] is None else row["completed_at"].isoformat(),
                "error": row["error"],
            }
        )

    return {
        "data": rows,
        "request_id": get_request_id(request),
        "timestamp": datetime.now(UTC).isoformat(),
    }


@router.get("/reembedding-status")
async def reembedding_status(
    request: Request,
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, object]:
    result = await session.execute(
        text(
            """
            SELECT
                id,
                task_name,
                status,
                total_rows,
                processed_rows,
                pct_complete,
                eta_seconds,
                started_at,
                completed_at,
                error
            FROM backfill_jobs
            WHERE task_name LIKE 'reembed_tenant:%'
            ORDER BY started_at DESC
            """
        )
    )
    rows = []
    for row in result.mappings().all():
        rows.append(
            {
                "id": str(row["id"]),
                "task_name": row["task_name"],
                "status": row["status"],
                "total_rows": row["total_rows"],
                "processed_rows": row["processed_rows"],
                "pct_complete": float(row["pct_complete"] or 0),
                "eta_seconds": row["eta_seconds"],
                "started_at": None if row["started_at"] is None else row["started_at"].isoformat(),
                "completed_at": None if row["completed_at"] is None else row["completed_at"].isoformat(),
                "error": row["error"],
            }
        )

    return {
        "data": rows,
        "request_id": get_request_id(request),
        "timestamp": datetime.now(UTC).isoformat(),
    }


@router.get("/queue-depth")
async def queue_depth(
    request: Request,
    session: AsyncSession = Depends(get_db_session),
    cache_service: CacheService = Depends(get_cache_service),
) -> dict[str, object]:
    snapshot = await QueueRouter(session=session, cache_service=cache_service).inspect_all_queues()
    return {
        "data": snapshot,
        "request_id": get_request_id(request),
        "timestamp": datetime.now(UTC).isoformat(),
    }


@router.get("/dead-letter-jobs")
async def dead_letter_jobs(
    request: Request,
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, object]:
    result = await session.execute(
        select(DeadLetterJob, ExtractionJob)
        .join(ExtractionJob, DeadLetterJob.job_id == ExtractionJob.id)
        .where(ExtractionJob.status == ExtractionJobStatus.dead)
        .order_by(DeadLetterJob.created_at.desc())
        .limit(100)
    )
    rows = []
    for row in _result_all_rows(result):
        dead_letter, job = _unpack_dead_letter_row(row)
        rows.append(
            {
                "id": str(job.id),
                "tenant_id": str(job.tenant_id),
                "proxy_user_id": str(job.proxy_user_id),
                "external_user_id": job.external_user_id,
                "status": job.status.value,
                "attempts": int(job.attempts or 0),
                "queue_name": job.queue_name,
                "error": (dead_letter.error if dead_letter is not None else None) or job.error,
                "queued_at": None
                if getattr(job, "created_at", None) is None and getattr(job, "queued_at", None) is None
                else (getattr(job, "created_at", None) or getattr(job, "queued_at", None)).isoformat(),
                "started_at": None
                if getattr(job, "processing_started_at", None) is None and getattr(job, "started_at", None) is None
                else (getattr(job, "processing_started_at", None) or getattr(job, "started_at", None)).isoformat(),
                "completed_at": None if job.completed_at is None else job.completed_at.isoformat(),
                "dead_lettered_at": None if job.dead_lettered_at is None else job.dead_lettered_at.isoformat(),
            }
        )
    return {
        "data": rows,
        "request_id": get_request_id(request),
        "timestamp": datetime.now(UTC).isoformat(),
    }


@router.post("/dead-letter-jobs/{job_id}/retry")
async def retry_dead_letter_job(
    job_id: str,
    request: Request,
    session: AsyncSession = Depends(get_db_session),
    cache_service: CacheService = Depends(get_cache_service),
) -> dict[str, object]:
    job = await session.get(ExtractionJob, uuid.UUID(job_id))
    if job is None or job.status != ExtractionJobStatus.dead:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="dead_letter_job_not_found")
    dead_letter = await session.execute(
        select(DeadLetterJob).where(DeadLetterJob.job_id == job.id)
    )
    dead_letter_row = _result_scalar_one_or_none(dead_letter)
    if dead_letter_row is not None and not isinstance(dead_letter_row, DeadLetterJob):
        dead_letter_row = None

    reservation = await QueueRouter(session=session, cache_service=cache_service).reserve_extraction_slot(
        tenant_id=str(job.tenant_id),
        job_id=str(job.id),
    )
    if reservation is None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="tenant_queue_limit_reached")

    queue_name = reservation.queue_name
    payload = dict(job.payload or {})
    payload["queue_name"] = queue_name
    payload["plan_tier"] = reservation.plan_tier
    payload["job_id"] = str(job.id)

    job.status = ExtractionJobStatus.queued
    job.queue_name = queue_name
    job.payload = payload
    job.celery_task_id = None
    job.attempts = 0
    job.memories_created = 0
    job.error = None
    job.stale_after = None
    job.processing_started_at = None
    job.started_at = None
    job.completed_at = None
    job.dead_lettered_at = None
    job.updated_at = datetime.now(UTC)
    if dead_letter_row is not None:
        dead_letter_row.last_retried_at = datetime.now(UTC)
        await session.flush()
    await session.commit()

    celery_app.send_task(EXTRACTION_TASK_NAME, args=[payload], queue=queue_name)
    return {
        "data": {
            "job_id": str(job.id),
            "status": "queued",
            "queue_name": queue_name,
        },
        "request_id": get_request_id(request),
        "timestamp": datetime.now(UTC).isoformat(),
    }


@router.post("/embedding-models/activate/{model_id}")
async def activate_embedding_model(
    model_id: str,
    request: Request,
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, object]:
    service = EmbeddingService(async_session=session)
    try:
        record = await service.set_active_model(model_id)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(exc),
        ) from exc

    return {
        "data": {
            "id": record.id,
            "provider": record.provider,
            "model_name": record.model_name,
            "dimensions": record.dimensions,
            "qdrant_collection": record.qdrant_collection,
            "is_active": record.is_active,
            "deprecated_at": record.deprecated_at,
            "created_at": record.created_at,
        },
        "request_id": get_request_id(request),
        "timestamp": datetime.now(UTC).isoformat(),
    }


@router.post("/backfill/run/proxy-user-ids")
async def trigger_backfill_proxy_user_ids(
    request: Request,
    batch_size: int = 1000,
    sleep_between_batches_ms: int = 100,
) -> dict[str, object]:
    if batch_size <= 0:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="batch_size must be greater than zero",
        )
    if sleep_between_batches_ms < 0:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="sleep_between_batches_ms must be zero or greater",
        )

    task = run_backfill_proxy_user_ids.delay(
        batch_size=batch_size,
        sleep_between_batches_ms=sleep_between_batches_ms,
    )
    return {
        "data": {
            "task_name": "backfill_proxy_user_ids",
            "task_id": task.id,
            "status": "queued",
            "batch_size": batch_size,
            "sleep_between_batches_ms": sleep_between_batches_ms,
        },
        "request_id": get_request_id(request),
        "timestamp": datetime.now(UTC).isoformat(),
    }
