from __future__ import annotations

from datetime import UTC
from datetime import datetime

from celery import shared_task
from celery.schedules import crontab
from sqlalchemy import select

from api.db.database import build_sync_session_factory
from api.db.models import ExtractionJob


REDACT_EXTRACTION_PAYLOADS_TASK_NAME = "api.tasks.provenance_tasks.redact_expired_extraction_payloads"
PROVENANCE_TASK_BEAT_SCHEDULE = {
    "redact-expired-extraction-payloads": {
        "task": REDACT_EXTRACTION_PAYLOADS_TASK_NAME,
        "schedule": crontab(hour=3, minute=20),
    }
}


def redact_job_payload(payload: dict) -> dict:
    redacted = dict(payload or {})
    redacted.pop("messages", None)
    redacted["messages_redacted"] = True
    return redacted


@shared_task(name=REDACT_EXTRACTION_PAYLOADS_TASK_NAME)
def redact_expired_extraction_payloads(batch_size: int = 1000) -> dict[str, int]:
    session_factory = build_sync_session_factory()
    session = session_factory()
    now = datetime.now(UTC)
    redacted_count = 0
    try:
        jobs = session.execute(
            select(ExtractionJob)
            .where(
                ExtractionJob.raw_payload_expires_at.is_not(None),
                ExtractionJob.raw_payload_expires_at <= now,
                ExtractionJob.payload_redacted_at.is_(None),
            )
            .order_by(ExtractionJob.raw_payload_expires_at.asc())
            .limit(max(1, min(int(batch_size), 5000)))
        ).scalars().all()
        for job in jobs:
            job.payload = redact_job_payload(dict(job.payload or {}))
            job.result = redact_job_payload(dict(job.result or {}))
            job.payload_redacted_at = now
            if job.dead_letter_entry is not None:
                job.dead_letter_entry.payload = redact_job_payload(
                    dict(job.dead_letter_entry.payload or {})
                )
            session.add(job)
            redacted_count += 1
        session.commit()
        return {"redacted_jobs": redacted_count}
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
