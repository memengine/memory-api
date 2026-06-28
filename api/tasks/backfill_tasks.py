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
from sqlalchemy import create_engine
from sqlalchemy import select
from sqlalchemy import text
from sqlalchemy import tuple_
from sqlalchemy.orm import Session
from sqlalchemy.orm import selectinload

from api.db.database import get_sync_database_url
from api.settings import get_settings
from api.db.models import GlobalAgent
from api.db.models import Memory
from api.db.models import MemoryCategory
from api.db.models import MemoryClaim
from api.db.models import MemoryClaimRevision
from api.db.models import MemorySourceEvent
from api.db.models import PermissionGrant
from api.db.models import ProxyUser
from api.db.models import UniversalMemory
from api.db.models import UniversalMemoryClaim
from api.db.models import UniversalMemoryClaimRevision
from api.db.models import VerifiedOrgConnection
from api.services.claim_ledger_service import authority_priority
from api.services.claim_ledger_service import build_claim_identity
from api.services.claim_ledger_service import normalize_text
from api.services.claim_versions import CLAIM_SCHEMA_VERSION
from api.services.claim_versions import TENANT_BACKFILL_PROCESSOR_VERSION
from api.services.provenance_service import build_provenance_snapshot
from api.services.universal_claim_ledger_service import UniversalClaimLedgerService


LEGACY_TENANT_ID = "00000000-0000-0000-0000-000000000001"
BACKFILL_STATUS_TASK_NAME = "api.tasks.backfill_tasks.run_backfill_proxy_user_ids"
PASSPORT_PROVENANCE_BACKFILL_TASK_NAME = "api.tasks.backfill_tasks.run_backfill_universal_provenance"
PASSPORT_PROVENANCE_BACKFILL_REASON = "legacy passport provenance backfill"
TENANT_PROVENANCE_BACKFILL_TASK_NAME = "api.tasks.backfill_tasks.run_backfill_tenant_provenance"
TENANT_PROVENANCE_RECOVERED_REASON = "legacy_source_event_recovered"
TENANT_PROVENANCE_UNKNOWN_REASON = "legacy_unknown"
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
                    self._set_cursor(last_cursor)
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
                session.rollback()
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


def tenant_backfill_revision_status(
    *,
    memory_is_archived: bool,
    claim_active_value: str | None,
    incoming_value: str,
) -> str:
    if memory_is_archived:
        return "archived"
    if normalize_text(claim_active_value or "") == normalize_text(incoming_value):
        return "asserted"
    return "superseded"

class BackfillTenantProvenance(BackfillTask):
    """Create an honest claim ledger for legacy tenant memories without rewriting them."""

    table_name = "memories"
    task_name = "backfill_tenant_provenance"

    def __init__(self, *, dry_run: bool = False, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.dry_run = dry_run
        self._lock_key: str | None = None
        self._lock_token: str | None = None
        if dry_run:
            self.task_name = "backfill_tenant_provenance_dry_run"

    def _where_sql(self) -> str:
        return (
            "proxy_user_id IS NOT NULL AND NOT EXISTS ("
            "SELECT 1 FROM memory_claim_revisions mcr "
            "WHERE mcr.memory_id = memories.id"
            ")"
        )

    def run(self, batch_size: int = 250, sleep_between_batches_ms: int = 100) -> BackfillResult:
        lock_key = "backfill:tenant_provenance:lock"
        lock_token = str(uuid.uuid4())
        try:
            acquired = bool(self.redis_client.set(lock_key, lock_token, nx=True, ex=3600))
        except Exception:
            acquired = False
        if not acquired:
            return BackfillResult(
                task_name=self.task_name, status="skipped", total_rows=0, processed_rows=0,
                pct_complete=0.0, eta_seconds=None, last_cursor=self._get_cursor(),
                error="Another tenant provenance backfill is already running.",
            )
        self._lock_key = lock_key
        self._lock_token = lock_token
        try:
            result = super().run(
                batch_size=min(max(batch_size, 1), 1000),
                sleep_between_batches_ms=max(sleep_between_batches_ms, 0),
            )
            if result.status == "complete":
                try:
                    self.redis_client.delete(self.cursor_key)
                except Exception:
                    pass
            return result
        finally:
            try:
                if self.redis_client.get(lock_key) == lock_token:
                    self.redis_client.delete(lock_key)
            except Exception:
                pass

    def process_batch(self, session: Session, batch_ids: list[Any]) -> int:
        if self._lock_key and self._lock_token:
            try:
                if self.redis_client.get(self._lock_key) == self._lock_token:
                    self.redis_client.expire(self._lock_key, 3600)
            except Exception:
                pass
        if not batch_ids:
            return 0

        rows = list(
            session.execute(
                select(Memory, ProxyUser.tenant_id)
                .join(ProxyUser, ProxyUser.id == Memory.proxy_user_id)
                .where(Memory.id.in_(batch_ids))
                .order_by(Memory.created_at, Memory.id)
            ).all()
        )
        if self.dry_run:
            return len(rows)

        memory_ids = [memory.id for memory, _tenant_id in rows]
        existing_memory_ids = set(
            session.execute(
                select(MemoryClaimRevision.memory_id).where(
                    MemoryClaimRevision.memory_id.in_(memory_ids)
                )
            ).scalars().all()
        )
        rows = [row for row in rows if row[0].id not in existing_memory_ids]
        if not rows:
            return 0

        source_event_ids = {
            memory.source_event_id for memory, _tenant_id in rows
            if memory.source_event_id is not None
        }
        events_by_id: dict[uuid.UUID, MemorySourceEvent] = {}
        if source_event_ids:
            events = session.execute(
                select(MemorySourceEvent)
                .where(MemorySourceEvent.id.in_(source_event_ids))
                .options(selectinload(MemorySourceEvent.writer))
            ).scalars().all()
            events_by_id = {event.id: event for event in events}

        prepared: list[tuple[Memory, uuid.UUID, Any, dict[str, Any] | None]] = []
        for memory, tenant_id in rows:
            event = events_by_id.get(memory.source_event_id)
            provenance = build_provenance_snapshot(event) if event is not None else None
            identity = build_claim_identity(
                content=memory.content,
                category=memory.category.value if hasattr(memory.category, "value") else str(memory.category),
                scope=(provenance or {}).get("scope") or {},
            )
            prepared.append((memory, tenant_id, identity, provenance))

        claim_keys = {
            (tenant_id, memory.proxy_user_id, identity.fingerprint)
            for memory, tenant_id, identity, _provenance in prepared
        }
        claims = session.execute(
            select(MemoryClaim).where(
                tuple_(
                    MemoryClaim.tenant_id,
                    MemoryClaim.proxy_user_id,
                    MemoryClaim.claim_fingerprint,
                ).in_(claim_keys)
            )
        ).scalars().all()
        claims_by_key = {
            (claim.tenant_id, claim.proxy_user_id, claim.claim_fingerprint): claim
            for claim in claims
        }

        inserted = 0
        for memory, tenant_id, identity, provenance in prepared:
            key = (tenant_id, memory.proxy_user_id, identity.fingerprint)
            claim = claims_by_key.get(key)
            category = memory.category if isinstance(memory.category, MemoryCategory) else MemoryCategory(str(memory.category))
            priority = authority_priority(provenance, category.value)
            event = events_by_id.get(memory.source_event_id)
            observed_at = event.observed_at if event is not None else None
            evidence_refs = list((provenance or {}).get("evidence") or [])
            if memory.source_conversation_id is not None:
                evidence_refs.append(
                    {"source_type": "conversation", "reference": str(memory.source_conversation_id)}
                )
            reason = TENANT_PROVENANCE_RECOVERED_REASON if provenance is not None else TENANT_PROVENANCE_UNKNOWN_REASON

            if claim is None:
                claim = MemoryClaim(
                    tenant_id=tenant_id,
                    proxy_user_id=memory.proxy_user_id,
                    category=category,
                    claim_fingerprint=identity.fingerprint,
                    subject_key=identity.subject_key,
                    predicate_key=identity.predicate_key,
                    scope=identity.scope,
                    active_value=None if memory.is_archived else identity.value,
                    status="archived" if memory.is_archived else "active",
                    active_memory_id=None if memory.is_archived else memory.id,
                    authority_priority=priority,
                    confidence_score=float(memory.confidence_score or 0.0),
                    observed_at=observed_at,
                    effective_at=memory.created_at or datetime.now(UTC),
                )
                session.add(claim)
                session.flush()
                claims_by_key[key] = claim
                revision_status = "archived" if memory.is_archived else "activated"
            else:
                # Historical data never replaces a winner established by live traffic or user correction.
                revision_status = tenant_backfill_revision_status(
                    memory_is_archived=memory.is_archived,
                    claim_active_value=claim.active_value,
                    incoming_value=identity.value,
                )

            revision = MemoryClaimRevision(
                claim_id=claim.id,
                memory_id=memory.id,
                source_event_id=memory.source_event_id if provenance is not None else None,
                source_writer_id=event.writer_id if event is not None else None,
                asserted_value=identity.value,
                status=revision_status,
                authority_priority=priority,
                confidence_score=float(memory.confidence_score or 0.0),
                observed_at=observed_at,
                evidence_refs=evidence_refs,
                resolution_reason=reason,
                schema_version=CLAIM_SCHEMA_VERSION,
                processor_version=TENANT_BACKFILL_PROCESSOR_VERSION,
                created_at=memory.created_at or datetime.now(UTC),
            )
            session.add(revision)
            session.flush()
            if revision_status == "activated":
                claim.winning_revision_id = revision.id
            inserted += 1
        return inserted


@shared_task(name=TENANT_PROVENANCE_BACKFILL_TASK_NAME)
def run_backfill_tenant_provenance(
    batch_size: int = 250, sleep_between_batches_ms: int = 100, dry_run: bool = True,
) -> dict[str, Any]:
    result = BackfillTenantProvenance(dry_run=dry_run).run(
        batch_size=batch_size, sleep_between_batches_ms=sleep_between_batches_ms,
    )
    return {
        "task_name": result.task_name, "status": result.status,
        "total_rows": result.total_rows, "processed_rows": result.processed_rows,
        "pct_complete": result.pct_complete, "eta_seconds": result.eta_seconds,
        "last_cursor": result.last_cursor, "paused_count": result.paused_count,
        "error": result.error, "dry_run": dry_run,
    }

class BackfillUniversalProvenance(BackfillTask):
    """Add claim-ledger provenance to legacy Passport memories without touching vectors."""

    table_name = "universal_memories"
    task_name = "backfill_universal_provenance"

    def __init__(self, *, dry_run: bool = False, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.dry_run = dry_run
        self._lock_key: str | None = None
        self._lock_token: str | None = None
        if dry_run:
            self.task_name = "backfill_universal_provenance_dry_run"

    def _where_sql(self) -> str:
        return (
            "NOT EXISTS ("
            "SELECT 1 FROM universal_memory_claim_revisions ucr "
            "WHERE ucr.universal_memory_id = universal_memories.id"
            ")"
        )

    def run(self, batch_size: int = 250, sleep_between_batches_ms: int = 100) -> BackfillResult:
        lock_key = "backfill:universal_provenance:lock"
        lock_token = str(uuid.uuid4())
        try:
            acquired = bool(self.redis_client.set(lock_key, lock_token, nx=True, ex=3600))
        except Exception:
            acquired = False
        if not acquired:
            return BackfillResult(
                task_name=self.task_name, status="skipped", total_rows=0, processed_rows=0,
                pct_complete=0.0, eta_seconds=None, last_cursor=self._get_cursor(),
                error="Another Passport provenance backfill is already running.",
            )

        self._lock_key = lock_key
        self._lock_token = lock_token

        try:
            result = super().run(
                batch_size=min(max(batch_size, 1), 1000),
                sleep_between_batches_ms=max(sleep_between_batches_ms, 0),
            )
            if result.status == "complete":
                try:
                    self.redis_client.delete(self.cursor_key)
                except Exception:
                    pass
            return result
        finally:
            try:
                if self.redis_client.get(lock_key) == lock_token:
                    self.redis_client.delete(lock_key)
            except Exception:
                pass

    def process_batch(self, session: Session, batch_ids: list[Any]) -> int:
        if self._lock_key and self._lock_token:
            try:
                if self.redis_client.get(self._lock_key) == self._lock_token:
                    self.redis_client.expire(self._lock_key, 3600)
            except Exception:
                pass
        if not batch_ids:
            return 0
        memories = list(session.execute(
            select(UniversalMemory).where(UniversalMemory.id.in_(batch_ids))
            .order_by(UniversalMemory.created_at, UniversalMemory.id)
        ).scalars().all())
        if self.dry_run:
            return len(memories)

        memory_ids = [memory.id for memory in memories]
        existing_memory_ids = set(session.execute(
            select(UniversalMemoryClaimRevision.universal_memory_id).where(
                UniversalMemoryClaimRevision.universal_memory_id.in_(memory_ids)
            )
        ).scalars().all())
        memories = [memory for memory in memories if memory.id not in existing_memory_ids]
        if not memories:
            return 0

        identities = {
            memory.id: build_claim_identity(
                content=memory.content,
                category=memory.category.value if hasattr(memory.category, "value") else str(memory.category),
                scope={},
            ) for memory in memories
        }
        claim_keys = {(memory.user_uui_id, identities[memory.id].fingerprint) for memory in memories}
        claims = list(session.execute(
            select(UniversalMemoryClaim).where(
                tuple_(UniversalMemoryClaim.user_uui_id, UniversalMemoryClaim.claim_fingerprint).in_(claim_keys)
            )
        ).scalars().all())
        claim_by_key = {(claim.user_uui_id, claim.claim_fingerprint): claim for claim in claims}

        revisions_by_claim: dict[uuid.UUID, list[UniversalMemoryClaimRevision]] = {}
        if claims:
            revisions = list(session.execute(
                select(UniversalMemoryClaimRevision)
                .where(UniversalMemoryClaimRevision.claim_id.in_([claim.id for claim in claims]))
                .order_by(UniversalMemoryClaimRevision.created_at.desc())
            ).scalars().all())
            for revision in revisions:
                revisions_by_claim.setdefault(revision.claim_id, []).append(revision)

        agent_pairs = {(memory.user_uui_id, memory.source_agent_id) for memory in memories if memory.source_agent_id is not None}
        grants_by_pair: dict[tuple[uuid.UUID, uuid.UUID], PermissionGrant] = {}
        if agent_pairs:
            grants = session.execute(select(PermissionGrant).where(
                tuple_(PermissionGrant.user_uui_id, PermissionGrant.agent_id).in_(agent_pairs)
            )).scalars().all()
            grants_by_pair = {(grant.user_uui_id, grant.agent_id): grant for grant in grants}

        agent_ids = {memory.source_agent_id for memory in memories if memory.source_agent_id is not None}
        tenant_by_agent: dict[uuid.UUID, uuid.UUID] = {}
        if agent_ids:
            tenant_by_agent = dict(session.execute(
                select(GlobalAgent.id, GlobalAgent.owner_tenant_id).where(GlobalAgent.id.in_(agent_ids))
            ).all())

        connection_ids = {memory.source_org_connection_id for memory in memories if memory.source_org_connection_id is not None}
        tenant_by_connection: dict[uuid.UUID, uuid.UUID] = {}
        if connection_ids:
            tenant_by_connection = dict(session.execute(
                select(VerifiedOrgConnection.id, VerifiedOrgConnection.tenant_id).where(
                    VerifiedOrgConnection.id.in_(connection_ids)
                )
            ).all())

        inserted = 0
        for memory in memories:
            identity = identities[memory.id]
            key = (memory.user_uui_id, identity.fingerprint)
            claim = claim_by_key.get(key)
            claim_revisions = revisions_by_claim.get(claim.id, []) if claim is not None else []
            current_revision = None
            if claim is not None and claim.winning_revision_id is not None:
                current_revision = next((revision for revision in claim_revisions if revision.id == claim.winning_revision_id), None)
            if current_revision is None and claim_revisions:
                current_revision = claim_revisions[0]
            claim_is_backfill_managed = claim is None or (
                bool(claim_revisions) and all(
                    revision.resolution_reason == PASSPORT_PROVENANCE_BACKFILL_REASON
                    for revision in claim_revisions
                )
            )
            grant = grants_by_pair.get((memory.user_uui_id, memory.source_agent_id)) if memory.source_agent_id is not None else None
            source_tenant_id = tenant_by_agent.get(memory.source_agent_id) if memory.source_agent_id is not None else None
            if source_tenant_id is None and memory.source_org_connection_id is not None:
                source_tenant_id = tenant_by_connection.get(memory.source_org_connection_id)

            _, claim, revision = UniversalClaimLedgerService.backfill_revision_sync(
                session, memory, claim=claim, current_revision=current_revision,
                claim_is_backfill_managed=claim_is_backfill_managed, grant=grant,
                source_tenant_id=source_tenant_id,
                resolution_reason=PASSPORT_PROVENANCE_BACKFILL_REASON,
            )
            claim_by_key[key] = claim
            revisions_by_claim.setdefault(claim.id, []).insert(0, revision)
            inserted += 1
        return inserted


@shared_task(name=PASSPORT_PROVENANCE_BACKFILL_TASK_NAME)
def run_backfill_universal_provenance(
    batch_size: int = 250, sleep_between_batches_ms: int = 100, dry_run: bool = True,
) -> dict[str, Any]:
    result = BackfillUniversalProvenance(dry_run=dry_run).run(
        batch_size=batch_size, sleep_between_batches_ms=sleep_between_batches_ms,
    )
    return {
        "task_name": result.task_name, "status": result.status,
        "total_rows": result.total_rows, "processed_rows": result.processed_rows,
        "pct_complete": result.pct_complete, "eta_seconds": result.eta_seconds,
        "last_cursor": result.last_cursor, "paused_count": result.paused_count,
        "error": result.error, "dry_run": dry_run,
    }
