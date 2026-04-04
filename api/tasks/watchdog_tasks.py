from __future__ import annotations

import logging
import uuid
from datetime import UTC
from datetime import datetime

from celery import shared_task
from sqlalchemy import update
from sqlalchemy.orm import Session
from sqlalchemy.orm import sessionmaker

from api.db.database import build_sync_session_factory
from api.db.models import DeadLetterJob
from api.db.models import ExtractionJob
from api.db.models import ExtractionJobStatus
from api.tasks.extraction_tasks import process_extraction_job
from api.tasks.queue_router import get_extraction_queue_sync
from api.tasks.queue_router import release_extraction_slot_sync


LOGGER = logging.getLogger("memoryos.watchdog")
WATCHDOG_TASK_NAME = "api.tasks.watchdog_tasks.check_stale_jobs"
WATCHDOG_BEAT_SCHEDULE = {
    "requeue-stale-extraction-jobs": {
        "task": WATCHDOG_TASK_NAME,
        "schedule": 120.0,
    }
}


def build_watchdog_session_factory() -> sessionmaker[Session]:
    return build_sync_session_factory()


def _mark_dead_without_requeue(session: Session, job: ExtractionJob, *, attempts: int) -> None:
    dead_letter = session.query(DeadLetterJob).filter(DeadLetterJob.job_id == job.id).one_or_none()
    if dead_letter is None:
        dead_letter = DeadLetterJob(
            job_id=job.id,
            tenant_id=job.tenant_id,
            proxy_user_id=job.proxy_user_id,
        )
    dead_letter.celery_task_id = job.celery_task_id
    dead_letter.attempts = attempts
    dead_letter.payload = dict(job.payload or {})
    dead_letter.error = "stale_processing_max_attempts_exceeded"
    session.add(dead_letter)

    job.status = ExtractionJobStatus.dead
    job.attempts = attempts
    job.error = "stale_processing_max_attempts_exceeded"
    job.dead_lettered_at = datetime.now(UTC)
    job.celery_task_id = None
    job.stale_after = None
    job.updated_at = datetime.now(UTC)
    session.add(job)
    session.commit()
    release_extraction_slot_sync(
        tenant_id=str(job.tenant_id),
        queue_name=str(job.queue_name) if job.queue_name else None,
        job_id=str(job.id),
    )


def run_watchdog_cycle(*, session_factory: sessionmaker[Session] | None = None) -> dict[str, int]:
    session_factory = session_factory or build_watchdog_session_factory()
    session = session_factory()
    checked = 0
    requeued = 0
    dead = 0
    try:
        rows = (
            session.query(ExtractionJob)
            .filter(
                ExtractionJob.status == ExtractionJobStatus.processing,
                ExtractionJob.stale_after.is_not(None),
                ExtractionJob.stale_after < datetime.now(UTC),
            )
            .order_by(ExtractionJob.stale_after.asc())
            .limit(100)
            .all()
        )
        for row in rows:
            checked += 1
            current_attempts = int(row.attempts or 0) + 1
            max_attempts = int(row.max_attempts or 3)
            if current_attempts > max_attempts:
                _mark_dead_without_requeue(session, row, attempts=current_attempts)
                dead += 1
                continue

            update_result = session.execute(
                update(ExtractionJob)
                .where(
                    ExtractionJob.id == row.id,
                    ExtractionJob.status == ExtractionJobStatus.processing,
                    ExtractionJob.stale_after == row.stale_after,
                )
                .values(
                    status=ExtractionJobStatus.queued,
                    attempts=current_attempts,
                    celery_task_id=None,
                    stale_after=None,
                    error="stale_processing_requeued",
                    updated_at=datetime.now(UTC),
                )
            )
            session.commit()
            if int(update_result.rowcount or 0) != 1:
                continue

            queue_name = get_extraction_queue_sync(tenant_id=str(row.tenant_id), session_factory=session_factory)
            payload = dict(row.payload or {})
            payload["job_id"] = str(row.id)
            payload["queue_name"] = queue_name
            process_extraction_job.apply_async(args=[payload], queue=queue_name)
            LOGGER.warning(
                "stale_job_requeued job_id=%s tenant_id=%s attempts=%s",
                row.id,
                row.tenant_id,
                current_attempts,
            )
            requeued += 1
        return {"checked": checked, "requeued": requeued, "dead": dead}
    finally:
        session.close()


@shared_task(name=WATCHDOG_TASK_NAME)
def check_stale_jobs() -> dict[str, int]:
    return run_watchdog_cycle()


__all__ = [
    "WATCHDOG_BEAT_SCHEDULE",
    "WATCHDOG_TASK_NAME",
    "check_stale_jobs",
    "run_watchdog_cycle",
]
