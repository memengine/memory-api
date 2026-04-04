from __future__ import annotations

import logging
import os
from collections.abc import Callable
from typing import Any

from celery import shared_task
from celery.schedules import crontab
from qdrant_client.http import models as qmodels
import sentry_sdk
from sqlalchemy import select
from sqlalchemy.orm import Session
from sqlalchemy.orm import joinedload
from sqlalchemy.orm import sessionmaker

from api.db.database import build_sync_session_factory
from api.db.models import EmbeddingModel
from api.db.models import Memory
from api.db.vector_store import QdrantService
from api.services.embedding_service import EmbeddingService
from api.services.vector_outbox import build_vector_payload
from api.services.vector_outbox import enqueue_vector_upsert


LOGGER = logging.getLogger(__name__)
RECONCILIATION_TASK_NAME = "api.tasks.reconciliation_tasks.reconcile_vector_store"
RECONCILIATION_TASK_BEAT_SCHEDULE = {
    "reconcile-vector-store": {
        "task": RECONCILIATION_TASK_NAME,
        "schedule": crontab(hour=4, minute=0),
    }
}
def build_reconciliation_session_factory() -> sessionmaker[Session]:
    return build_sync_session_factory()


def run_reconciliation_cycle(
    *,
    session_factory: Callable[[], Any] | None = None,
    qdrant_service: QdrantService | None = None,
    client: Any | None = None,
    page_size: int = 200,
) -> dict[str, int]:
    factory = session_factory or build_reconciliation_session_factory()
    session = factory()
    qdrant = qdrant_service or QdrantService()
    summary = {
        "checked": 0,
        "missing_in_qdrant": 0,
        "orphan_in_qdrant": 0,
        "repaired": 0,
    }

    try:
        missing_memories = _find_missing_memories(session, qdrant, summary, page_size=page_size)
        if summary["missing_in_qdrant"] > 100:
            sentry_sdk.capture_message(
                f"vector_reconciliation_large_drift missing_in_qdrant={summary['missing_in_qdrant']}",
                level="fatal",
            )
            missing_memories = []

        embedding_service = EmbeddingService(sync_session=session, gemini_client=client)
        repaired_missing = _enqueue_missing_repair(session, missing_memories, embedding_service)
        summary["repaired"] += repaired_missing
        repaired_orphans = _remove_orphan_vectors(session, qdrant, summary, page_size=page_size)
        summary["repaired"] += repaired_orphans
        session.commit()

        LOGGER.info("vector_reconciliation_summary %s", summary)
        return summary
    finally:
        if hasattr(session, "close"):
            session.close()


def _find_missing_memories(
    session: Session,
    qdrant: QdrantService,
    summary: dict[str, int],
    *,
    page_size: int,
) -> list[Memory]:
    missing: list[Memory] = []
    last_id: Any | None = None

    while True:
        stmt = (
            select(Memory)
            .options(joinedload(Memory.proxy_user), joinedload(Memory.embedding_model))
            .where(Memory.is_archived.is_(False))
            .order_by(Memory.id)
            .limit(page_size)
        )
        if last_id is not None:
            stmt = stmt.where(Memory.id > last_id)

        batch = list(session.execute(stmt).scalars().all())
        if not batch:
            break

        summary["checked"] += len(batch)
        grouped_ids: dict[str, list[Memory]] = {}
        for memory in batch:
            collection_name = (
                memory.embedding_model.qdrant_collection
                if getattr(memory, "embedding_model", None) is not None
                else qdrant.COLLECTION_NAME
            )
            grouped_ids.setdefault(str(collection_name), []).append(memory)

        for collection_name, memories in grouped_ids.items():
            memory_ids = [str(memory.id) for memory in memories]
            retrieved = qdrant.breaker.call_sync(
                qdrant.client.retrieve,
                collection_name=collection_name,
                ids=memory_ids,
                with_payload=False,
                with_vectors=False,
            )
            found_ids = {str(point.id) for point in (retrieved or [])}
            for memory in memories:
                if str(memory.id) not in found_ids:
                    summary["missing_in_qdrant"] += 1
                    if summary["missing_in_qdrant"] <= 100:
                        missing.append(memory)

        last_id = batch[-1].id

    return missing


def _enqueue_missing_repair(
    session: Session,
    memories: list[Memory],
    embedding_service: EmbeddingService,
) -> int:
    if not memories:
        return 0

    repaired = 0
    for memory in memories:
        tenant_id = str(memory.proxy_user.tenant_id) if memory.proxy_user else None
        proxy_user_id = str(memory.proxy_user_id) if memory.proxy_user_id else None
        embedding = embedding_service.embed_sync(memory.content, model_id=memory.embedding_model_id)
        enqueue_vector_upsert(
            session,
            memory_id=memory.id,
            embedding=embedding.vector,
            payload=build_vector_payload(
                memory,
                tenant_id=tenant_id,
                proxy_user_id=proxy_user_id,
                user_id=str(memory.user_id),
                embedding_model_id=embedding.model_id,
                qdrant_collection=embedding.qdrant_collection,
            ),
        )
        repaired += 1
    return repaired


def _remove_orphan_vectors(
    session: Session,
    qdrant: QdrantService,
    summary: dict[str, int],
    *,
    page_size: int,
) -> int:
    repaired = 0
    collection_names = [
        str(value)
        for value in session.execute(select(EmbeddingModel.qdrant_collection).distinct()).scalars().all()
        if value
    ] or [qdrant.COLLECTION_NAME]

    for collection_name in collection_names:
        next_offset = None

        while True:
            points, next_offset = qdrant.breaker.call_sync(
                qdrant.client.scroll,
                collection_name=collection_name,
                scroll_filter=None,
                limit=page_size,
                offset=next_offset,
                with_payload=False,
                with_vectors=False,
            )
            if not points:
                break

            point_ids = [str(point.id) for point in points]
            existing_ids = {
                str(value)
                for value in session.execute(
                    select(Memory.id).where(
                        Memory.id.in_(point_ids),  # type: ignore[arg-type]
                        Memory.is_archived.is_(False),
                    )
                ).scalars().all()
            }
            orphan_ids = [point_id for point_id in point_ids if point_id not in existing_ids]
            summary["orphan_in_qdrant"] += len(orphan_ids)
            if orphan_ids:
                qdrant.breaker.call_sync(
                    qdrant.client.delete,
                    collection_name=collection_name,
                    points_selector=qmodels.PointIdsList(points=orphan_ids),
                    wait=True,
                )
                repaired += len(orphan_ids)

            if next_offset is None:
                break

    return repaired


@shared_task(name=RECONCILIATION_TASK_NAME)
def reconcile_vector_store() -> dict[str, int]:
    return run_reconciliation_cycle()
