from __future__ import annotations

from datetime import UTC
from datetime import datetime
import re
from typing import Any

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


_SAFE_JOB_FIELDS = {
    "job_id", "tenant_id", "proxy_user_id", "external_user_id", "user_uui_id",
    "agent_id", "source_event_id", "queue_name", "attempt", "attempts", "status",
}
_SAFE_REFERENCE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")


def _redact_source(source: Any) -> dict[str, Any]:
    if not isinstance(source, dict):
        return {}
    safe = {
        key: source[key]
        for key in ("service", "event_id", "observed_at", "payload_hash", "payload_hash_version")
        if source.get(key) is not None
    }
    evidence: list[dict[str, Any]] = []
    for item in source.get("evidence") or []:
        if not isinstance(item, dict):
            continue
        entry = {key: item[key] for key in ("source_type", "content_hash") if item.get(key) is not None}
        reference = item.get("reference")
        if isinstance(reference, str) and _SAFE_REFERENCE.fullmatch(reference):
            entry["reference"] = reference
        elif reference is not None:
            entry["reference_redacted"] = True
        evidence.append(entry)
    if evidence:
        safe["evidence"] = evidence
    return safe


def redact_job_payload(payload: dict) -> dict[str, Any]:
    """Retain operational provenance while removing expired customer text."""
    payload = dict(payload or {})
    redacted = {key: payload[key] for key in _SAFE_JOB_FIELDS if payload.get(key) is not None}
    source = _redact_source(payload.get("source"))
    if source:
        redacted["source"] = source
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
