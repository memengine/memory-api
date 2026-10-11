from __future__ import annotations

import logging
import uuid
import time
from datetime import UTC
from datetime import datetime
from datetime import timedelta

from celery import shared_task
from sqlalchemy import or_
from sqlalchemy import update
from sqlalchemy import select, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session
from sqlalchemy.orm import sessionmaker

from api.db.database import build_sync_session_factory
from api.db.database import get_database_url, get_sync_database_url
from api.db.models import DeadLetterJob
from api.db.models import ExtractionJob
from api.db.models import ExtractionJobStatus
from api.db.models import Tenant, ProxyUser, ApiKey
from api.tasks.extraction_tasks import process_extraction_job
from api.tasks.queue_router import get_extraction_queue_sync
from api.tasks.queue_router import release_extraction_slot_sync
from api.services.proxy_user_service import ProxyUserService


LOGGER = logging.getLogger("memoryos.watchdog")
WATCHDOG_TASK_NAME = "api.tasks.watchdog_tasks.check_stale_jobs"
WATCHDOG_BEAT_SCHEDULE = {
    "requeue-stale-extraction-jobs": {
        "task": WATCHDOG_TASK_NAME,
        "schedule": 120.0,
    }
}
QUEUED_DISPATCH_GRACE = timedelta(seconds=60)
DISPATCH_RECOVERY_TASK_NAME = "api.tasks.watchdog_tasks.recover_durable_dispatch"
DISPATCH_RECOVERY_BEAT_SCHEDULE = {
    "recover-durable-extraction-dispatch": {"task": DISPATCH_RECOVERY_TASK_NAME, "schedule": 5.0},
}


def _publish_durable_reference(payload: dict, task_id: str, queue_name: str) -> None:
    # Fail rather than enter Celery's publish retry loop. Lost acknowledgements
    # are safe: the next cycle uses exactly the same worker delivery identity.
    with process_extraction_job.app.connection_for_write(transport_options={
        "socket_connect_timeout": 1.0, "socket_timeout": 1.0,
        "retry_on_timeout": False,
    }) as connection:
        connection.ensure_connection(max_retries=0, timeout=1.0)
        process_extraction_job.apply_async(args=[payload], queue=queue_name,
            task_id=task_id, connection=connection, retry=False)


def run_durable_dispatch_recovery(*, session_factory=None, publisher=None) -> dict[str, int]:
    owned_factory = session_factory is None
    if owned_factory:
        url = make_url(get_sync_database_url(get_database_url()))
        url = url.update_query_dict({"connect_timeout": "1",
            "options": f"{url.query.get('options', '')} -c statement_timeout=1000 -c lock_timeout=500".strip()})
        session_factory = build_sync_session_factory(url.render_as_string(hide_password=False))
    publisher = publisher or _publish_durable_reference
    result = {"checked": 0, "published": 0, "skipped": 0, "errors": 0}
    deadline = time.monotonic() + 3.0
    try:
        # Candidate selection is read-only. Each publication locks/rechecks one
        # row, so a stalled send never locks the entire batch or creates a job.
        with session_factory() as session:
            session.execute(text("SET LOCAL statement_timeout = '1s'"))
            ids = session.scalars(select(ExtractionJob.id).where(
                ExtractionJob.request_key_sha256.is_not(None),
                ExtractionJob.request_binding.is_not(None),
                ExtractionJob.status == ExtractionJobStatus.queued,
                ExtractionJob.processing_started_at.is_(None),
            ).order_by(ExtractionJob.updated_at, ExtractionJob.queued_at).limit(100)).all()
        for job_id in ids:
            if time.monotonic() >= deadline:
                break
            with session_factory() as session:
                try:
                    session.execute(text("SET LOCAL statement_timeout = '1s'"))
                    session.execute(text("SET LOCAL lock_timeout = '500ms'"))
                    row = session.scalars(select(ExtractionJob).where(
                        ExtractionJob.id == job_id,
                        ExtractionJob.status == ExtractionJobStatus.queued,
                        ExtractionJob.processing_started_at.is_(None),
                        ExtractionJob.request_key_sha256.is_not(None),
                    ).with_for_update(skip_locked=True)).one_or_none()
                    if row is None:
                        continue
                    result["checked"] += 1
                    tenant = session.scalars(select(Tenant).where(Tenant.id == row.tenant_id)
                        .with_for_update(read=True)).one_or_none()
                    owner = session.scalars(select(ProxyUser).where(ProxyUser.id == row.proxy_user_id)
                        .with_for_update(read=True)).one_or_none()
                    key_id = (row.payload or {}).get("source", {}).get("api_key_id")
                    key = session.scalars(select(ApiKey).where(ApiKey.id == uuid.UUID(key_id))
                        .with_for_update(read=True)).one_or_none() if key_id else None
                    permissions = {str(p).strip().lower() for p in (key.permissions or [])} if key else set()
                    if (tenant is None or not tenant.is_active or owner is None or owner.is_blocked
                            or owner.tenant_id != row.tenant_id
                            or not isinstance(row.request_binding, dict)
                            or owner.external_user_id_hash != ProxyUserService.hash_external_user_id(
                                str(row.tenant_id), str(row.request_binding.get("owner_id", "")))
                            or (key_id and (key is None or not key.is_active or key.tenant_id != row.tenant_id
                                           or not permissions.intersection({"write", "admin"})))):
                        row.updated_at = datetime.now(UTC)
                        session.commit()
                        result["skipped"] += 1
                        continue
                    # Commit identity before publication. The row remains queued;
                    # later cycles retry a missing acknowledgement with this ID.
                    if not row.celery_task_id:
                        row.celery_task_id = str(row.id)
                        session.commit()
                        # Next cycle rechecks permissions and locks the row
                        # afresh; never publish after releasing those checks.
                        continue
                    payload = {"job_id": str(row.id), "tenant_id": str(row.tenant_id),
                        "queue_name": row.queue_name, "_payload_reference": "extraction_job"}
                    publisher(payload, str(row.celery_task_id), row.queue_name)
                    row.updated_at = datetime.now(UTC)
                    session.commit()
                    result["published"] += 1
                except Exception:
                    session.rollback()
                    result["errors"] += 1
                    LOGGER.exception("durable_dispatch_recovery_failed job_id=%s", job_id)
        return result
    finally:
        if owned_factory:
            session_factory.kw["bind"].dispose()


@shared_task(name=DISPATCH_RECOVERY_TASK_NAME)
def recover_durable_dispatch() -> dict[str, int]:
    return run_durable_dispatch_recovery()


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
        now = datetime.now(UTC)
        queued_cutoff = now - QUEUED_DISPATCH_GRACE
        rows = (
            session.query(ExtractionJob)
            .filter(
                or_(
                    (
                        (ExtractionJob.status == ExtractionJobStatus.processing)
                        & ExtractionJob.stale_after.is_not(None)
                        & (ExtractionJob.stale_after < now)
                    ),
                    (
                        (ExtractionJob.status == ExtractionJobStatus.queued)
                        & ExtractionJob.celery_task_id.is_(None)
                        & ExtractionJob.processing_started_at.is_(None)
                        & (ExtractionJob.queued_at < queued_cutoff)
                    ),
                )
            )
            .order_by(ExtractionJob.queued_at.asc())
            .limit(100)
            .all()
        )
        for row in rows:
            checked += 1
            if row.status == ExtractionJobStatus.queued:
                recovery_task_id = str(uuid.uuid4())
                update_result = session.execute(
                    update(ExtractionJob)
                    .where(
                        ExtractionJob.id == row.id,
                        ExtractionJob.status == ExtractionJobStatus.queued,
                        ExtractionJob.celery_task_id.is_(None),
                        ExtractionJob.processing_started_at.is_(None),
                        ExtractionJob.queued_at < queued_cutoff,
                    )
                    .values(
                        celery_task_id=recovery_task_id,
                        error="queued_dispatch_recovered",
                        updated_at=now,
                    )
                )
                session.commit()
                if int(update_result.rowcount or 0) != 1:
                    continue

                queue_name = str(row.queue_name or "").strip() or get_extraction_queue_sync(
                    tenant_id=str(row.tenant_id),
                    session_factory=session_factory,
                )
                payload = {
                    "job_id": str(row.id),
                    "tenant_id": str(row.tenant_id),
                    "queue_name": queue_name,
                    "_payload_reference": "extraction_job",
                }
                try:
                    process_extraction_job.apply_async(
                        args=[payload],
                        queue=queue_name,
                        task_id=recovery_task_id,
                    )
                except Exception:
                    session.execute(
                        update(ExtractionJob)
                        .where(
                            ExtractionJob.id == row.id,
                            ExtractionJob.status == ExtractionJobStatus.queued,
                            ExtractionJob.celery_task_id == recovery_task_id,
                        )
                        .values(celery_task_id=None, updated_at=datetime.now(UTC))
                    )
                    session.commit()
                    LOGGER.exception("queued_job_recovery_dispatch_failed job_id=%s", row.id)
                    continue
                LOGGER.warning(
                    "queued_job_dispatch_recovered job_id=%s tenant_id=%s queue=%s",
                    row.id,
                    row.tenant_id,
                    queue_name,
                )
                requeued += 1
                continue

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
            payload = {
                "job_id": str(row.id),
                "tenant_id": str(row.tenant_id),
                "queue_name": queue_name,
                "_payload_reference": "extraction_job",
            }
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
