from __future__ import annotations

import math
import os
import time
import uuid
from dataclasses import dataclass
from datetime import UTC
from datetime import datetime
from typing import Any

import psutil
import redis
from celery import shared_task
from sqlalchemy import bindparam
from sqlalchemy import create_engine
from sqlalchemy import func
from sqlalchemy import select
from sqlalchemy import table
from sqlalchemy import column
from sqlalchemy import text
from sqlalchemy.orm import Session

from api.db.database import get_sync_database_url
from api.settings import get_settings


LEGACY_TENANT_ID = "00000000-0000-0000-0000-000000000001"
BACKFILL_STATUS_TASK_NAME = "api.tasks.backfill_tasks.run_backfill_proxy_user_ids"
DEFAULT_ACTIVE_QUERY_THRESHOLD = 20
DEFAULT_CPU_THRESHOLD = 70.0
DEFAULT_PAUSE_SECONDS = 30.0


@dataclass(slots=True)
class BackfillResult:
    task_name: str
    status: str
    total_rows: int
    processed_rows: int
    pct_complete: float
    eta_seconds: int | None
    last_cursor: str | None
    paused_count: int = 0
    error: str | None = None


class BackfillTask:
    task_name: str = "base"
    table_name: str = ""
    id_column: str = "id"
    active_query_threshold: int = DEFAULT_ACTIVE_QUERY_THRESHOLD
    cpu_threshold: float = DEFAULT_CPU_THRESHOLD
    pause_seconds: float = DEFAULT_PAUSE_SECONDS

    def __init__(
        self,
        *,
        engine: Any | None = None,
        redis_client: Any | None = None,
    ) -> None:
        self.engine = engine or create_engine(get_sync_database_url(), future=True, pool_pre_ping=True)
        redis_url = os.getenv("REDIS_URL") or get_settings().redis_url
        if redis_client is None and not redis_url:
            raise RuntimeError("REDIS_URL is required.")
        self.redis_client = redis_client or redis.from_url(
            redis_url,
            encoding="utf-8",
            decode_responses=True,
            socket_connect_timeout=0.2,
            socket_timeout=0.2,
        )

    @property
    def cursor_key(self) -> str:
        return f"backfill:{self.task_name}:cursor"

    def run(self, batch_size: int = 1000, sleep_between_batches_ms: int = 100) -> BackfillResult:
        started_at = datetime.now(UTC)
        processed_rows = 0
        paused_count = 0
        last_cursor = self._get_cursor()

        with Session(self.engine) as session:
            job_id = self._start_job(session)
            total_rows = self._count_remaining_rows(session, last_cursor)
            self._update_job_progress(
                session,
                job_id=job_id,
                status="running",
                total_rows=total_rows,
                processed_rows=0,
                started_at=started_at,
                completed_at=None,
                error=None,
            )
            session.commit()

            try:
                while True:
                    if self._should_pause(session):
                        paused_count += 1
                        self._update_job_progress(
                            session,
                            job_id=job_id,
                            status="paused",
                            total_rows=total_rows,
                            processed_rows=processed_rows,
                            started_at=started_at,
                            completed_at=None,
                            error=None,
                        )
                        session.commit()
                        time.sleep(self.pause_seconds)
                        continue

                    batch_ids = self._select_batch_ids(session, last_cursor, batch_size)
                    if not batch_ids:
                        break

                    batch_processed = self.process_batch(session, batch_ids)
                    last_cursor = batch_ids[-1]
                    processed_rows += batch_processed
                    self._set_cursor(last_cursor)

                    eta_seconds = self._estimate_eta_seconds(
                        started_at=started_at,
                        processed_rows=processed_rows,
                        total_rows=total_rows,
                    )
                    self._update_job_progress(
                        session,
                        job_id=job_id,
                        status="running",
                        total_rows=total_rows,
                        processed_rows=processed_rows,
                        started_at=started_at,
                        completed_at=None,
                        error=None,
                        eta_seconds=eta_seconds,
                    )
                    session.commit()
                    time.sleep(max(sleep_between_batches_ms, 0) / 1000)

                self._update_job_progress(
                    session,
                    job_id=job_id,
                    status="complete",
                    total_rows=total_rows,
                    processed_rows=processed_rows,
                    started_at=started_at,
                    completed_at=datetime.now(UTC),
                    error=None,
                )
                session.commit()
                return BackfillResult(
                    task_name=self.task_name,
                    status="complete",
                    total_rows=total_rows,
                    processed_rows=processed_rows,
                    pct_complete=100.0 if total_rows == 0 else min((processed_rows / total_rows) * 100.0, 100.0),
                    eta_seconds=0,
                    last_cursor=last_cursor,
                    paused_count=paused_count,
                )
            except Exception as exc:
                self._update_job_progress(
                    session,
                    job_id=job_id,
                    status="failed",
                    total_rows=total_rows,
                    processed_rows=processed_rows,
                    started_at=started_at,
                    completed_at=datetime.now(UTC),
                    error=str(exc),
                )
                session.commit()
                return BackfillResult(
                    task_name=self.task_name,
                    status="failed",
                    total_rows=total_rows,
                    processed_rows=processed_rows,
                    pct_complete=0.0 if total_rows == 0 else min((processed_rows / total_rows) * 100.0, 100.0),
                    eta_seconds=None,
                    last_cursor=last_cursor,
                    paused_count=paused_count,
                    error=str(exc),
                )

    def process_batch(self, session: Session, batch_ids: list[Any]) -> int:
        raise NotImplementedError

    def _where_sql(self) -> str:
        raise NotImplementedError

    def _select_batch_ids(self, session: Session, cursor: str | None, batch_size: int) -> list[str]:
        stmt = text(
            f"""
            SELECT {self.id_column}
            FROM {self.table_name}
            WHERE {self._where_sql()}
              AND {self.id_column} > CAST(:cursor AS uuid)
            ORDER BY {self.id_column}
            LIMIT :limit
            """
        ) if cursor else text(
            f"""
            SELECT {self.id_column}
            FROM {self.table_name}
            WHERE {self._where_sql()}
            ORDER BY {self.id_column}
            LIMIT :limit
            """
        )

        params: dict[str, Any] = {"limit": batch_size}
        if cursor:
            params["cursor"] = cursor
        rows = session.execute(stmt, params).scalars().all()
        return [str(value) for value in rows]

    def _count_remaining_rows(self, session: Session, cursor: str | None) -> int:
        stmt = text(
            f"""
            SELECT COUNT(*)
            FROM {self.table_name}
            WHERE {self._where_sql()}
              AND {self.id_column} > CAST(:cursor AS uuid)
            """
        ) if cursor else text(
            f"""
            SELECT COUNT(*)
            FROM {self.table_name}
            WHERE {self._where_sql()}
            """
        )
        params = {"cursor": cursor} if cursor else {}
        return int(session.execute(stmt, params).scalar_one())

    def _get_cursor(self) -> str | None:
        try:
            return self.redis_client.get(self.cursor_key)
        except Exception:
            return None

    def _set_cursor(self, cursor: str) -> None:
        try:
            self.redis_client.set(self.cursor_key, cursor)
        except Exception:
            return None

    def _should_pause(self, session: Session) -> bool:
        try:
            cpu = psutil.cpu_percent(interval=1)
        except Exception:
            cpu = 0.0
        try:
            active_queries = int(
                session.execute(
                    text(
                        """
                        SELECT count(*)
                        FROM pg_stat_activity
                        WHERE state = 'active'
                        """
                    )
                ).scalar_one()
            )
        except Exception:
            active_queries = 0
        return cpu > self.cpu_threshold or active_queries > self.active_query_threshold

    def _start_job(self, session: Session) -> str:
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
            {"id": job_id, "task_name": self.task_name},
        )
        return job_id

    def _update_job_progress(
        self,
        session: Session,
        *,
        job_id: str,
        status: str,
        total_rows: int,
        processed_rows: int,
        started_at: datetime,
        completed_at: datetime | None,
        error: str | None,
        eta_seconds: int | None = None,
    ) -> None:
        pct_complete = 100.0 if total_rows == 0 else min((processed_rows / total_rows) * 100.0, 100.0)
        eta = eta_seconds
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
                "eta_seconds": eta,
                "completed_at": completed_at,
                "error": error,
            },
        )

    @staticmethod
    def _estimate_eta_seconds(*, started_at: datetime, processed_rows: int, total_rows: int) -> int | None:
        if processed_rows <= 0 or total_rows <= processed_rows:
            return 0 if total_rows == processed_rows else None
        elapsed_seconds = max((datetime.now(UTC) - started_at).total_seconds(), 1.0)
        rows_per_second = processed_rows / elapsed_seconds
        if rows_per_second <= 0:
            return None
        remaining_rows = total_rows - processed_rows
        return int(math.ceil(remaining_rows / rows_per_second))


class BackfillProxyUserIds(BackfillTask):
    task_name = "backfill_proxy_user_ids"
    table_name = "memories"

    def _where_sql(self) -> str:
        return "proxy_user_id IS NULL"

    def process_batch(self, session: Session, batch_ids: list[Any]) -> int:
        if not batch_ids:
            return 0

        session.execute(
            text(
                """
                INSERT INTO tenants (id, company_name, plan_tier, is_active, created_at, metadata)
                VALUES (CAST(:tenant_id AS uuid), 'Legacy User Migration', 'starter', TRUE, NOW(), '{}'::jsonb)
                ON CONFLICT (id) DO NOTHING
                """
            ),
            {"tenant_id": LEGACY_TENANT_ID},
        )

        session.execute(
            text(
                """
                WITH batch_memories AS (
                    SELECT
                        m.id,
                        m.user_id,
                        COALESCE(u.external_id, m.user_id::text) AS external_user_id,
                        encode(
                            digest(
                                convert_to(
                                    :tenant_id || ':' || COALESCE(u.external_id, m.user_id::text),
                                    'UTF8'
                                ),
                                'sha256'
                            ),
                            'hex'
                        ) AS external_user_id_hash
                    FROM memories m
                    LEFT JOIN users u ON u.id = m.user_id
                    WHERE m.id = ANY(CAST(:memory_ids AS uuid[]))
                      AND m.proxy_user_id IS NULL
                )
                INSERT INTO proxy_users (
                    tenant_id,
                    external_user_id,
                    external_user_id_hash,
                    metadata
                )
                SELECT
                    CAST(:tenant_id AS uuid),
                    batch_memories.external_user_id,
                    batch_memories.external_user_id_hash,
                    jsonb_build_object(
                        'migration_source', 'backfill_proxy_user_ids',
                        'legacy_user_id', batch_memories.user_id::text
                    )
                FROM batch_memories
                ON CONFLICT (tenant_id, external_user_id_hash)
                DO UPDATE SET last_active_at = NOW()
                """
            ),
            {"tenant_id": LEGACY_TENANT_ID, "memory_ids": batch_ids},
        )

        result = session.execute(
            text(
                """
                WITH batch_memories AS (
                    SELECT
                        m.id,
                        m.user_id,
                        encode(
                            digest(
                                convert_to(
                                    :tenant_id || ':' || COALESCE(u.external_id, m.user_id::text),
                                    'UTF8'
                                ),
                                'sha256'
                            ),
                            'hex'
                        ) AS external_user_id_hash
                    FROM memories m
                    LEFT JOIN users u ON u.id = m.user_id
                    WHERE m.id = ANY(CAST(:memory_ids AS uuid[]))
                      AND m.proxy_user_id IS NULL
                )
                UPDATE memories AS m
                SET proxy_user_id = pu.id
                FROM batch_memories
                JOIN proxy_users pu
                  ON pu.tenant_id = CAST(:tenant_id AS uuid)
                 AND pu.external_user_id_hash = batch_memories.external_user_id_hash
                WHERE m.id = batch_memories.id
                  AND m.proxy_user_id IS NULL
                RETURNING m.id
                """
            ),
            {"tenant_id": LEGACY_TENANT_ID, "memory_ids": batch_ids},
        )
        updated_ids = [str(value) for value in result.scalars().all()]

        session.execute(
            text(
                """
                UPDATE proxy_users AS pu
                SET memory_count = counts.memory_count
                FROM (
                    SELECT proxy_user_id, COUNT(*)::integer AS memory_count
                    FROM memories
                    WHERE proxy_user_id IN (
                        SELECT proxy_user_id
                        FROM memories
                        WHERE id = ANY(CAST(:memory_ids AS uuid[]))
                    )
                    GROUP BY proxy_user_id
                ) AS counts
                WHERE pu.id = counts.proxy_user_id
                """
            ),
            {"memory_ids": batch_ids},
        )
        return len(updated_ids)


@shared_task(name=BACKFILL_STATUS_TASK_NAME)
def run_backfill_proxy_user_ids(batch_size: int = 1000, sleep_between_batches_ms: int = 100) -> dict[str, Any]:
    result = BackfillProxyUserIds().run(
        batch_size=batch_size,
        sleep_between_batches_ms=sleep_between_batches_ms,
    )
    return {
        "task_name": result.task_name,
        "status": result.status,
        "total_rows": result.total_rows,
        "processed_rows": result.processed_rows,
        "pct_complete": result.pct_complete,
        "eta_seconds": result.eta_seconds,
        "last_cursor": result.last_cursor,
        "paused_count": result.paused_count,
        "error": result.error,
    }
