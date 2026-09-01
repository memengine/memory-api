from __future__ import annotations

import uuid
from types import SimpleNamespace

from api.db.models import VectorSyncOperation, VectorSyncOutbox, VectorSyncStatus
from api.services.vector_outbox import enqueue_vector_archive, enqueue_vector_delete
from api.tasks.vector_sync_tasks import _apply_archive_batch


class _Session:
    def __init__(self) -> None:
        self.rows = []

    def add(self, row) -> None:
        self.rows.append(row)


def test_supersession_archive_and_privacy_delete_remain_distinct_operations() -> None:
    session = _Session()
    memory_id = uuid.uuid4()
    enqueue_vector_archive(
        session,
        memory_id=memory_id,
        payload={"is_archived": True, "lifecycle_state": "superseded"},
    )
    enqueue_vector_delete(session, memory_id=memory_id, payload={"privacy_delete": True})

    assert [row.operation for row in session.rows] == [
        VectorSyncOperation.archive,
        VectorSyncOperation.delete,
    ]


def test_archive_worker_updates_payload_without_replacing_or_deleting_vector() -> None:
    calls = []
    row = VectorSyncOutbox(
        id=uuid.uuid4(), operation=VectorSyncOperation.archive,
        memory_id=uuid.uuid4(), embedding=None,
        payload={"is_archived": True, "lifecycle_state": "superseded"},
        status=VectorSyncStatus.processing, attempts=1,
    )
    qdrant = SimpleNamespace(
        COLLECTION_NAME="memories", VECTOR_SIZE=1536,
        client=SimpleNamespace(set_payload=lambda **kwargs: calls.append(kwargs)),
        breaker=SimpleNamespace(call_sync=lambda fn, *args, **kwargs: fn(*args, **kwargs)),
        _ensure_collection_if_possible=lambda *_args: None,
    )

    _apply_archive_batch(qdrant, [row])

    assert len(calls) == 1
    assert calls[0]["points"] == [str(row.memory_id)]
    assert calls[0]["payload"]["lifecycle_state"] == "superseded"
    assert not hasattr(qdrant.client, "delete")
    assert not hasattr(qdrant.client, "upsert")
