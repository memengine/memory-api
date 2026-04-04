from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import Any

from sqlalchemy import create_engine
from sqlalchemy import select
from sqlalchemy.orm import Session
from sqlalchemy.orm import joinedload
from google import genai
from dotenv import load_dotenv
from qdrant_client.http import models as qmodels

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

load_dotenv(PROJECT_ROOT / ".env")

from api.db.database import get_sync_database_url
from api.db.models import Memory
from api.db.models import ProxyUser
from api.db.models import VectorSyncOutbox
from api.db.models import VectorSyncOperation
from api.db.models import VectorSyncStatus
from api.db.vector_store import QdrantService
from api.services.vector_outbox import build_vector_payload
from api.services.vector_outbox import enqueue_vector_upsert
from api.tasks.reconciliation_tasks import _build_embedder
from api.tasks.vector_sync_tasks import run_outbox_cycle


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="One-time repair tool for PostgreSQL/Qdrant drift."
    )
    parser.add_argument(
        "--page-size",
        type=int,
        default=200,
        help="Batch size for scanning PostgreSQL and Qdrant.",
    )
    parser.add_argument(
        "--repair-missing",
        action="store_true",
        help="Enqueue outbox upserts for memories present in PostgreSQL but missing in Qdrant.",
    )
    parser.add_argument(
        "--delete-orphans",
        action="store_true",
        help="Delete vectors that exist in Qdrant without an active PostgreSQL memory row.",
    )
    parser.add_argument(
        "--process-outbox",
        action="store_true",
        help="Run outbox cycles after enqueuing missing repairs until the queue is empty.",
    )
    parser.add_argument(
        "--max-missing-repairs",
        type=int,
        default=None,
        help="Optional cap on how many missing PostgreSQL memories to enqueue in one run.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Report drift only. Do not enqueue repairs or delete vectors.",
    )
    return parser.parse_args()


def scan_missing_memories(
    session: Session,
    qdrant: QdrantService,
    *,
    page_size: int,
) -> list[Memory]:
    missing: list[Memory] = []
    last_id: Any | None = None

    while True:
        stmt = (
            select(Memory)
            .options(joinedload(Memory.proxy_user))
            .where(Memory.is_archived.is_(False))
            .order_by(Memory.id)
            .limit(page_size)
        )
        if last_id is not None:
            stmt = stmt.where(Memory.id > last_id)

        batch = list(session.execute(stmt).scalars().all())
        if not batch:
            break

        memory_ids = [str(memory.id) for memory in batch]
        retrieved = qdrant.client.retrieve(
            collection_name=qdrant.COLLECTION_NAME,
            ids=memory_ids,
            with_payload=False,
            with_vectors=False,
        )
        found_ids = {str(point.id) for point in (retrieved or [])}
        for memory in batch:
            if str(memory.id) not in found_ids:
                missing.append(memory)

        last_id = batch[-1].id

    return missing


def scan_orphan_vector_ids(
    session: Session,
    qdrant: QdrantService,
    *,
    page_size: int,
) -> list[str]:
    orphan_ids: list[str] = []
    next_offset = None

    while True:
        points, next_offset = qdrant.client.scroll(
            collection_name=qdrant.COLLECTION_NAME,
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
        orphan_ids.extend(point_id for point_id in point_ids if point_id not in existing_ids)
        if next_offset is None:
            break

    return orphan_ids


def enqueue_missing_repairs(
    session: Session,
    client: genai.Client,
    memories: list[Memory],
    *,
    max_repairs: int | None,
) -> int:
    embed_text = _build_embedder(client)
    enqueued = 0

    for memory in memories:
        if max_repairs is not None and enqueued >= max_repairs:
            break

        already_pending = session.execute(
            select(VectorSyncOutbox.id).where(
                VectorSyncOutbox.memory_id == memory.id,
                VectorSyncOutbox.operation == VectorSyncOperation.upsert,
                VectorSyncOutbox.status.in_([VectorSyncStatus.pending, VectorSyncStatus.processing]),
            )
        ).first()
        if already_pending:
            continue

        tenant_id = str(memory.proxy_user.tenant_id) if memory.proxy_user else None
        proxy_user_id = str(memory.proxy_user_id) if memory.proxy_user_id else None
        embedding = embed_text(memory.content)
        enqueue_vector_upsert(
            session,
            memory_id=memory.id,
            embedding=embedding,
            payload=build_vector_payload(
                memory,
                tenant_id=tenant_id,
                proxy_user_id=proxy_user_id,
                user_id=str(memory.user_id),
            ),
        )
        enqueued += 1

    return enqueued


def delete_orphan_vectors(qdrant: QdrantService, orphan_ids: list[str], *, page_size: int) -> int:
    deleted = 0
    for start in range(0, len(orphan_ids), page_size):
        batch = orphan_ids[start : start + page_size]
        if not batch:
            continue
        qdrant.client.delete(
            collection_name=qdrant.COLLECTION_NAME,
            points_selector=qmodels.PointIdsList(points=batch),
            wait=True,
        )
        deleted += len(batch)
    return deleted


def process_outbox_until_empty() -> dict[str, int]:
    total = {"cycles": 0, "claimed": 0, "done": 0, "failed": 0}
    while True:
        result = run_outbox_cycle()
        total["cycles"] += 1
        total["claimed"] += result["claimed"]
        total["done"] += result["done"]
        total["failed"] += result["failed"]
        if result["claimed"] == 0:
            break
    return total


def main() -> None:
    args = parse_args()
    engine = create_engine(get_sync_database_url(), future=True)
    qdrant = QdrantService()

    with Session(engine) as session:
        missing = scan_missing_memories(session, qdrant, page_size=args.page_size)
        orphans = scan_orphan_vector_ids(session, qdrant, page_size=args.page_size)

        summary = {
            "missing_in_qdrant": len(missing),
            "orphan_in_qdrant": len(orphans),
            "enqueued_missing_repairs": 0,
            "deleted_orphans": 0,
            "outbox_cycles": 0,
            "outbox_done": 0,
            "outbox_failed": 0,
        }

        if args.dry_run:
            print(summary)
            return

        if args.repair_missing:
            client = genai.Client(api_key=os.getenv("GEMINI_API_KEY"))
            summary["enqueued_missing_repairs"] = enqueue_missing_repairs(
                session,
                client,
                missing,
                max_repairs=args.max_missing_repairs,
            )
            session.commit()

        if args.delete_orphans:
            summary["deleted_orphans"] = delete_orphan_vectors(qdrant, orphans, page_size=args.page_size)

        if args.process_outbox:
            outbox_summary = process_outbox_until_empty()
            summary["outbox_cycles"] = outbox_summary["cycles"]
            summary["outbox_done"] = outbox_summary["done"]
            summary["outbox_failed"] = outbox_summary["failed"]

        print(summary)


if __name__ == "__main__":
    main()
