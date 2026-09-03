from __future__ import annotations

import logging
import os
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import sentry_sdk
from celery import shared_task
from celery.signals import worker_process_shutdown
from qdrant_client.http import models as qmodels
from sqlalchemy import select, update
from sqlalchemy.orm import Session, sessionmaker

from api.db.database import build_sync_session_factory
from api.db.models import VectorSyncOperation, VectorSyncOutbox, VectorSyncStatus
from api.db.vector_store import QdrantService

LOGGER = logging.getLogger(__name__)
PROCESS_OUTBOX_TASK_NAME = "api.tasks.vector_sync_tasks.process_outbox"
DEFAULT_QDRANT_SYNC_CHUNK_SIZE = 10
VECTOR_SYNC_TASK_BEAT_SCHEDULE = {
    "process-vector-sync-outbox": {
        "task": PROCESS_OUTBOX_TASK_NAME,
        "schedule": 5.0,
    }
}
_VECTOR_SYNC_SESSION_FACTORY: sessionmaker[Session] | None = None
_VECTOR_SYNC_SESSION_FACTORY_PID: int | None = None


def build_vector_sync_session_factory() -> sessionmaker[Session]:
    global _VECTOR_SYNC_SESSION_FACTORY
    global _VECTOR_SYNC_SESSION_FACTORY_PID

    process_id = os.getpid()
    if (
        _VECTOR_SYNC_SESSION_FACTORY is None
        or _VECTOR_SYNC_SESSION_FACTORY_PID != process_id
    ):
        dispose_vector_sync_session_factory()
        _VECTOR_SYNC_SESSION_FACTORY = build_sync_session_factory()
        _VECTOR_SYNC_SESSION_FACTORY_PID = process_id
    return _VECTOR_SYNC_SESSION_FACTORY


@worker_process_shutdown.connect
def dispose_vector_sync_session_factory(**_extra: Any) -> None:
    """Dispose the process-local vector-outbox engine when a Celery child exits."""
    global _VECTOR_SYNC_SESSION_FACTORY
    global _VECTOR_SYNC_SESSION_FACTORY_PID

    factory = _VECTOR_SYNC_SESSION_FACTORY
    _VECTOR_SYNC_SESSION_FACTORY = None
    _VECTOR_SYNC_SESSION_FACTORY_PID = None
    if factory is None:
        return
    engine = factory.kw.get("bind")
    if engine is not None:
        engine.dispose()


def run_outbox_cycle(
    *,
    session_factory: Callable[[], Any] | None = None,
    qdrant_service: QdrantService | None = None,
    batch_size: int = 100,
) -> dict[str, int]:
    factory = session_factory or build_vector_sync_session_factory()
    session = factory()
    qdrant = qdrant_service or QdrantService()

    try:
        pending_ids = list(
            session.execute(
                select(VectorSyncOutbox.id)
                .where(VectorSyncOutbox.status == VectorSyncStatus.pending)
                .order_by(VectorSyncOutbox.created_at, VectorSyncOutbox.id)
                .limit(batch_size)
            ).scalars().all()
        )
        claimed_rows: list[VectorSyncOutbox] = []
        for outbox_id in pending_ids:
            claimed = _claim_outbox_row(session, outbox_id)
            if claimed is not None:
                claimed_rows.append(claimed)
        session.commit()

        if not claimed_rows:
            return {"claimed": 0, "done": 0, "failed": 0}

        upsert_rows = [row for row in claimed_rows if row.operation == VectorSyncOperation.upsert]
        delete_rows = [row for row in claimed_rows if row.operation == VectorSyncOperation.delete]
        archive_rows = [row for row in claimed_rows if row.operation == VectorSyncOperation.archive]
        done = 0
        failed = 0

        if upsert_rows:
            done_delta, failed_delta = _process_chunks(
                session,
                qdrant,
                upsert_rows,
                chunk_size=DEFAULT_QDRANT_SYNC_CHUNK_SIZE,
                apply_fn=_apply_upsert_batch,
            )
            done += done_delta
            failed += failed_delta

        if archive_rows:
            done_delta, failed_delta = _process_chunks(
                session,
                qdrant,
                archive_rows,
                chunk_size=DEFAULT_QDRANT_SYNC_CHUNK_SIZE,
                apply_fn=_apply_archive_batch,
            )
            done += done_delta
            failed += failed_delta

        if delete_rows:
            done_delta, failed_delta = _process_chunks(
                session,
                qdrant,
                delete_rows,
                chunk_size=DEFAULT_QDRANT_SYNC_CHUNK_SIZE,
                apply_fn=_apply_delete_batch,
            )
            done += done_delta
            failed += failed_delta

        session.commit()
        return {"claimed": len(claimed_rows), "done": done, "failed": failed}
    finally:
        if hasattr(session, "close"):
            session.close()


def _claim_outbox_row(session: Session, outbox_id: Any) -> VectorSyncOutbox | None:
    claimed = session.execute(
        update(VectorSyncOutbox)
        .where(
            VectorSyncOutbox.id == outbox_id,
            VectorSyncOutbox.status == VectorSyncStatus.pending,
        )
        .values(
            status=VectorSyncStatus.processing,
            attempts=VectorSyncOutbox.attempts + 1,
            last_error=None,
        )
        .returning(VectorSyncOutbox)
    ).scalar_one_or_none()
    return claimed


def _apply_upsert_batch(qdrant: QdrantService, rows: list[VectorSyncOutbox]) -> None:
    grouped_rows: dict[str, list[VectorSyncOutbox]] = {}
    for row in rows:
        collection_name = str((row.payload or {}).get("qdrant_collection") or qdrant.COLLECTION_NAME)
        grouped_rows.setdefault(collection_name, []).append(row)

    ensure_collection = getattr(qdrant, "_ensure_collection_if_possible", None)
    for collection_name, grouped in grouped_rows.items():
        vector_size = len(grouped[0].embedding or []) if grouped and grouped[0].embedding else qdrant.VECTOR_SIZE
        if callable(ensure_collection):
            ensure_collection(collection_name, vector_size)
        points = [
            qmodels.PointStruct(
                id=str(row.memory_id),
                vector=list(row.embedding or []),
                payload=dict(row.payload or {}),
            )
            for row in grouped
        ]
        qdrant.breaker.call_sync(
            qdrant.client.upsert,
            collection_name=collection_name,
            points=points,
            wait=True,
        )


def _apply_archive_batch(qdrant: QdrantService, rows: list[VectorSyncOutbox]) -> None:
    grouped_rows: dict[str, list[VectorSyncOutbox]] = {}
    for row in rows:
        collection_name = str((row.payload or {}).get("qdrant_collection") or qdrant.COLLECTION_NAME)
        grouped_rows.setdefault(collection_name, []).append(row)

    ensure_collection = getattr(qdrant, "_ensure_collection_if_possible", None)
    for collection_name, grouped in grouped_rows.items():
        if callable(ensure_collection):
            ensure_collection(collection_name, qdrant.VECTOR_SIZE)
        for row in grouped:
            qdrant.breaker.call_sync(
                qdrant.client.set_payload,
                collection_name=collection_name,
                payload=dict(row.payload or {}),
                points=[str(row.memory_id)],
                wait=True,
            )

def _apply_delete_batch(qdrant: QdrantService, rows: list[VectorSyncOutbox]) -> None:
    grouped_rows: dict[str, list[VectorSyncOutbox]] = {}
    for row in rows:
        collection_name = str((row.payload or {}).get("qdrant_collection") or qdrant.COLLECTION_NAME)
        grouped_rows.setdefault(collection_name, []).append(row)

    ensure_collection = getattr(qdrant, "_ensure_collection_if_possible", None)
    for collection_name, grouped in grouped_rows.items():
        if callable(ensure_collection):
            ensure_collection(collection_name, qdrant.VECTOR_SIZE)
        point_ids = [str(row.memory_id) for row in grouped]
        qdrant.breaker.call_sync(
            qdrant.client.delete,
            collection_name=collection_name,
            points_selector=qmodels.PointIdsList(points=point_ids),
            wait=True,
        )


def _process_chunks(
    session: Session,
    qdrant: QdrantService,
    rows: list[VectorSyncOutbox],
    *,
    chunk_size: int,
    apply_fn: Callable[[QdrantService, list[VectorSyncOutbox]], None],
) -> tuple[int, int]:
    done = 0
    failed = 0
    for start in range(0, len(rows), chunk_size):
        chunk = rows[start : start + chunk_size]
        try:
            apply_fn(qdrant, chunk)
            _mark_rows_done(session, chunk)
            done += len(chunk)
        except Exception as exc:
            failed += _mark_rows_failed_or_pending(session, chunk, str(exc))
    return done, failed


def _mark_rows_done(session: Session, rows: list[VectorSyncOutbox]) -> None:
    row_ids = [row.id for row in rows]
    session.execute(
        update(VectorSyncOutbox)
        .where(VectorSyncOutbox.id.in_(row_ids))
        .values(
            status=VectorSyncStatus.done,
            synced_at=datetime.now(UTC),
            last_error=None,
        )
    )


def _mark_rows_failed_or_pending(session: Session, rows: list[VectorSyncOutbox], error_message: str) -> int:
    failed_count = 0
    for row in rows:
        refreshed = session.get(VectorSyncOutbox, row.id)
        attempts = int(getattr(refreshed, "attempts", row.attempts) or 0)
        privacy_delete = bool((getattr(refreshed, "payload", None) or {}).get("privacy_delete"))
        if attempts >= 3 and not privacy_delete:
            refreshed.status = VectorSyncStatus.failed
            failed_count += 1
            sentry_sdk.capture_message(
                f"vector_sync_failed memory_id={row.memory_id} error={error_message}",
                level="error",
            )
        else:
            refreshed.status = VectorSyncStatus.pending
        refreshed.last_error = error_message
        session.add(refreshed)
    return failed_count


@shared_task(name=PROCESS_OUTBOX_TASK_NAME)
def process_outbox() -> dict[str, int]:
    result = run_outbox_cycle()
    LOGGER.info(
        "vector_sync_outbox_cycle claimed=%s done=%s failed=%s",
        result["claimed"],
        result["done"],
        result["failed"],
    )
    return result
