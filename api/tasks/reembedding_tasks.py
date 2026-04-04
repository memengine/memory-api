from __future__ import annotations

import math
import os
import time
import uuid
from datetime import UTC
from datetime import datetime
from typing import Any

import redis
from celery import shared_task
from sqlalchemy import text
from sqlalchemy.orm import Session
from sqlalchemy.orm import sessionmaker

from api.db.database import build_sync_session_factory
from api.db.models import EmbeddingModel
from api.db.models import Memory
from api.db.models import ProxyUser
from api.db.vector_store import QdrantService
from api.services.embedding_service import EmbeddingService
from api.services.vector_outbox import build_vector_payload
from api.settings import get_settings


REEMBED_TASK_NAME = "api.tasks.reembedding_tasks.reembed_tenant"
DEFAULT_RATE_LIMIT_PER_SECOND = 50


def build_reembedding_session_factory() -> sessionmaker[Session]:
    return build_sync_session_factory()


def _redis_client() -> redis.Redis:
    return redis.Redis.from_url(
        os.getenv("REDIS_URL") or get_settings().redis_url or (_raise_missing_redis_url()),
        encoding="utf-8",
        decode_responses=True,
        socket_connect_timeout=0.2,
        socket_timeout=0.2,
    )


def _raise_missing_redis_url() -> str:
    raise RuntimeError("REDIS_URL is required.")


def _cursor_key(tenant_id: str, old_model_id: str) -> str:
    return f"reembed:{tenant_id}:{old_model_id}:cursor"


def _task_name(tenant_id: str, old_model_id: str, new_model_id: str) -> str:
    return f"reembed_tenant:{tenant_id}:{old_model_id}:{new_model_id}"


def _estimate_eta(started_at: datetime, processed_rows: int, total_rows: int) -> int | None:
    if processed_rows <= 0 or total_rows <= processed_rows:
        return 0 if total_rows == processed_rows else None
    elapsed_seconds = max((datetime.now(UTC) - started_at).total_seconds(), 1.0)
    rows_per_second = processed_rows / elapsed_seconds
    if rows_per_second <= 0:
        return None
    remaining_rows = total_rows - processed_rows
    return int(math.ceil(remaining_rows / rows_per_second))


def _latest_job(session: Session, task_name: str) -> dict[str, Any] | None:
    row = session.execute(
        text(
            """
            SELECT id, task_name, status, total_rows, processed_rows, started_at
            FROM backfill_jobs
            WHERE task_name = :task_name
            ORDER BY started_at DESC
            LIMIT 1
            """
        ),
        {"task_name": task_name},
    ).mappings().first()
    return None if row is None else dict(row)


def _latest_incomplete_job(session: Session, task_name: str) -> dict[str, Any] | None:
    row = session.execute(
        text(
            """
            SELECT id, task_name, status, total_rows, processed_rows, started_at
            FROM backfill_jobs
            WHERE task_name = :task_name
              AND status != 'complete'
            ORDER BY started_at DESC
            LIMIT 1
            """
        ),
        {"task_name": task_name},
    ).mappings().first()
    return None if row is None else dict(row)


def _create_job(session: Session, task_name: str) -> str:
    job_id = str(uuid.uuid4())
    session.execute(
        text(
            """
            INSERT INTO backfill_jobs (
                id, task_name, status, total_rows, processed_rows, started_at, completed_at, error
            ) VALUES (
                CAST(:id AS uuid), :task_name, 'running', 0, 0, NOW(), NULL, NULL
            )
            """
        ),
        {"id": job_id, "task_name": task_name},
    )
    return job_id


def _update_job(
    session: Session,
    *,
    job_id: str,
    status: str,
    total_rows: int,
    processed_rows: int,
    eta_seconds: int | None,
    completed_at: datetime | None = None,
    error: str | None = None,
) -> None:
    pct_complete = 100.0 if total_rows == 0 else min((processed_rows / total_rows) * 100.0, 100.0)
    session.execute(
        text(
            """
            UPDATE backfill_jobs
            SET status = :status,
                total_rows = :total_rows,
                processed_rows = :processed_rows,
                pct_complete = :pct_complete,
                eta_seconds = :eta_seconds,
                completed_at = :completed_at,
                error = :error
            WHERE id = CAST(:id AS uuid)
            """
        ),
        {
            "id": job_id,
            "status": status,
            "total_rows": total_rows,
            "processed_rows": processed_rows,
            "pct_complete": pct_complete,
            "eta_seconds": eta_seconds,
            "completed_at": completed_at,
            "error": error,
        },
    )


def _select_batch(
    session: Session,
    *,
    tenant_id: str,
    old_model_id: str,
    cursor: str | None,
    batch_size: int,
) -> list[dict[str, Any]]:
    if cursor:
        stmt = text(
            """
            SELECT
                m.id,
                m.content,
                m.user_id,
                m.proxy_user_id,
                pu.tenant_id
            FROM memories m
            JOIN proxy_users pu ON pu.id = m.proxy_user_id
            WHERE pu.tenant_id = CAST(:tenant_id AS uuid)
              AND m.embedding_model_id = :old_model_id
              AND m.is_archived = FALSE
              AND m.id > CAST(:cursor AS uuid)
            ORDER BY m.id
            LIMIT :limit
            """
        )
        params = {
            "tenant_id": tenant_id,
            "old_model_id": old_model_id,
            "cursor": cursor,
            "limit": batch_size,
        }
    else:
        stmt = text(
            """
            SELECT
                m.id,
                m.content,
                m.user_id,
                m.proxy_user_id,
                pu.tenant_id
            FROM memories m
            JOIN proxy_users pu ON pu.id = m.proxy_user_id
            WHERE pu.tenant_id = CAST(:tenant_id AS uuid)
              AND m.embedding_model_id = :old_model_id
              AND m.is_archived = FALSE
            ORDER BY m.id
            LIMIT :limit
            """
        )
        params = {"tenant_id": tenant_id, "old_model_id": old_model_id, "limit": batch_size}
    return [dict(row) for row in session.execute(stmt, params).mappings().all()]


def _remaining_count(session: Session, *, tenant_id: str, old_model_id: str, cursor: str | None) -> int:
    if cursor:
        stmt = text(
            """
            SELECT COUNT(*)
            FROM memories m
            JOIN proxy_users pu ON pu.id = m.proxy_user_id
            WHERE pu.tenant_id = CAST(:tenant_id AS uuid)
              AND m.embedding_model_id = :old_model_id
              AND m.is_archived = FALSE
              AND m.id > CAST(:cursor AS uuid)
            """
        )
        params = {"tenant_id": tenant_id, "old_model_id": old_model_id, "cursor": cursor}
    else:
        stmt = text(
            """
            SELECT COUNT(*)
            FROM memories m
            JOIN proxy_users pu ON pu.id = m.proxy_user_id
            WHERE pu.tenant_id = CAST(:tenant_id AS uuid)
              AND m.embedding_model_id = :old_model_id
              AND m.is_archived = FALSE
            """
        )
        params = {"tenant_id": tenant_id, "old_model_id": old_model_id}
    return int(session.execute(stmt, params).scalar_one())


def run_reembedding_cycle(
    tenant_id: str,
    old_model_id: str,
    new_model_id: str,
    *,
    batch_size: int = 50,
    session_factory: sessionmaker[Session] | None = None,
    redis_client: redis.Redis | None = None,
    qdrant_service: QdrantService | None = None,
    embedding_service: EmbeddingService | None = None,
) -> dict[str, Any]:
    factory = session_factory or build_reembedding_session_factory()
    redis_conn = redis_client or _redis_client()
    session = factory()
    qdrant = qdrant_service or QdrantService()
    embedding_service = embedding_service or EmbeddingService(sync_session=session)
    task_name = _task_name(tenant_id, old_model_id, new_model_id)
    cursor = redis_conn.get(_cursor_key(tenant_id, old_model_id))
    started_at = datetime.now(UTC)

    try:
        previous_job = _latest_incomplete_job(session, task_name)
        remaining_rows = _remaining_count(
            session,
            tenant_id=tenant_id,
            old_model_id=old_model_id,
            cursor=cursor,
        )
        if previous_job is not None:
            job_id = str(previous_job["id"])
            processed_rows = int(previous_job["processed_rows"] or 0)
            total_rows = max(int(previous_job["total_rows"] or 0), processed_rows + remaining_rows)
        else:
            job_id = _create_job(session, task_name)
            processed_rows = 0
            total_rows = remaining_rows
        _update_job(
            session,
            job_id=job_id,
            status="running",
            total_rows=total_rows,
            processed_rows=processed_rows,
            eta_seconds=None,
        )
        session.commit()

        old_model = embedding_service.get_model_sync(old_model_id)
        new_model = embedding_service.get_model_sync(new_model_id)

        window_started = time.monotonic()
        calls_in_window = 0

        while True:
            batch = _select_batch(
                session,
                tenant_id=tenant_id,
                old_model_id=old_model_id,
                cursor=cursor,
                batch_size=batch_size,
            )
            if not batch:
                break

            for row in batch:
                if calls_in_window >= DEFAULT_RATE_LIMIT_PER_SECOND:
                    elapsed = time.monotonic() - window_started
                    if elapsed < 1.0:
                        time.sleep(1.0 - elapsed)
                    window_started = time.monotonic()
                    calls_in_window = 0

                memory = session.get(Memory, row["id"])
                if memory is None or memory.embedding_model_id != old_model_id:
                    cursor = str(row["id"])
                    redis_conn.set(_cursor_key(tenant_id, old_model_id), cursor)
                    continue

                embedding = embedding_service.embed_sync(memory.content, model_id=new_model_id)
                payload = build_vector_payload(
                    memory,
                    tenant_id=tenant_id,
                    proxy_user_id=str(memory.proxy_user_id),
                    user_id=str(memory.user_id),
                    embedding_model_id=embedding.model_id,
                    qdrant_collection=embedding.qdrant_collection,
                )
                qdrant.upsert_memory(
                    str(memory.id),
                    embedding.vector,
                    payload,
                    collection_name=embedding.qdrant_collection,
                    vector_size=embedding.dimensions,
                )
                qdrant.delete_memory(str(memory.id), collection_name=old_model.qdrant_collection)
                memory.embedding_model_id = new_model.id
                session.add(memory)

                cursor = str(memory.id)
                redis_conn.set(_cursor_key(tenant_id, old_model_id), cursor)
                processed_rows += 1
                calls_in_window += 1

            session.commit()
            _update_job(
                session,
                job_id=job_id,
                status="running",
                total_rows=total_rows,
                processed_rows=processed_rows,
                eta_seconds=_estimate_eta(started_at, processed_rows, total_rows),
            )
            session.commit()

        _update_job(
            session,
            job_id=job_id,
            status="complete",
            total_rows=total_rows,
            processed_rows=processed_rows,
            eta_seconds=0,
            completed_at=datetime.now(UTC),
        )
        session.commit()
        redis_conn.delete(_cursor_key(tenant_id, old_model_id))
        return {
            "task_name": task_name,
            "status": "complete",
            "tenant_id": tenant_id,
            "old_model_id": old_model_id,
            "new_model_id": new_model_id,
            "processed_rows": processed_rows,
            "total_rows": total_rows,
            "last_cursor": cursor,
        }
    except Exception as exc:
        session.rollback()
        if "job_id" in locals():
            _update_job(
                session,
                job_id=job_id,
                status="failed",
                total_rows=total_rows if "total_rows" in locals() else 0,
                processed_rows=processed_rows if "processed_rows" in locals() else 0,
                eta_seconds=None,
                completed_at=datetime.now(UTC),
                error=str(exc),
            )
            session.commit()
        raise
    finally:
        session.close()


@shared_task(name=REEMBED_TASK_NAME)
def reembed_tenant(
    tenant_id: str,
    old_model_id: str,
    new_model_id: str,
    batch_size: int = 50,
) -> dict[str, Any]:
    return run_reembedding_cycle(
        tenant_id=tenant_id,
        old_model_id=old_model_id,
        new_model_id=new_model_id,
        batch_size=batch_size,
    )
