from __future__ import annotations

import sys
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy import select
from sqlalchemy.orm import Session
from qdrant_client.http.exceptions import UnexpectedResponse

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from api.db.database import get_sync_database_url
from api.db.models import Memory
from api.db.models import ProxyUser
from api.db.vector_store import QdrantService


BATCH_SIZE = 100


def iter_batches(items: list[tuple[str, str, str]], batch_size: int) -> list[list[tuple[str, str, str]]]:
    return [items[index : index + batch_size] for index in range(0, len(items), batch_size)]


def main() -> None:
    engine = create_engine(get_sync_database_url(), future=True)
    qdrant_service = QdrantService()

    with Session(engine) as session:
        rows = session.execute(
            select(Memory.id, ProxyUser.tenant_id, Memory.proxy_user_id)
            .join(ProxyUser, Memory.proxy_user_id == ProxyUser.id)
            .where(Memory.proxy_user_id.is_not(None))
        ).all()

    payload_rows = [(str(memory_id), str(tenant_id), str(proxy_user_id)) for memory_id, tenant_id, proxy_user_id in rows]
    if not payload_rows:
        print("No proxy-user-scoped memories found. Nothing to migrate.")
        return

    updated_count = 0
    skipped_missing = 0
    for batch in iter_batches(payload_rows, BATCH_SIZE):
        migrated_ids: list[str] = []
        for memory_id, tenant_id, proxy_user_id in batch:
            try:
                qdrant_service.client.set_payload(
                    collection_name=qdrant_service.COLLECTION_NAME,
                    payload={
                        "memory_id": memory_id,
                        "tenant_id": tenant_id,
                        "proxy_user_id": proxy_user_id,
                    },
                    points=[memory_id],
                    wait=True,
                )
                updated_count += 1
                migrated_ids.append(memory_id)
            except UnexpectedResponse as exc:
                if "No point with id" in str(exc):
                    skipped_missing += 1
                    continue
                raise
        if migrated_ids and hasattr(qdrant_service.client, "delete_payload"):
            qdrant_service.client.delete_payload(
                collection_name=qdrant_service.COLLECTION_NAME,
                keys=["user_id"],
                points=migrated_ids,
                wait=True,
            )
        print(
            f"Migrated {updated_count}/{len(payload_rows)} Qdrant payloads..."
            f" Skipped missing: {skipped_missing}"
        )

    print(
        f"Completed Qdrant payload migration for {updated_count} memories."
        f" Skipped missing points: {skipped_missing}."
    )


if __name__ == "__main__":
    main()
