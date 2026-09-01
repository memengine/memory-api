from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import MagicMock

from api.db.models import Memory
from api.db.models import MemoryCategory
from api.db.models import ProxyUser
from api.db.models import VectorSyncOperation
from api.db.models import VectorSyncOutbox
from api.db.models import VectorSyncStatus
from api.services.embedding_service import EmbeddingResult
from api.services.conflict_resolver import ConflictResolver
from api.services.extractor import ExtractedMemory
from api.tasks import reconciliation_tasks
from api.tasks import vector_sync_tasks


class FakeResult:
    def __init__(self, values):
        self._values = values

    def scalars(self):
        return self

    def all(self):
        return list(self._values)


class FakeOutboxSession:
    def __init__(self, rows: list[VectorSyncOutbox]) -> None:
        self.rows = {row.id: row for row in rows}
        self.commits = 0
        self.closed = False

    def execute(self, _statement):
        pending_ids = [
            row.id
            for row in self.rows.values()
            if row.status == VectorSyncStatus.pending
        ]
        pending_ids.sort(key=str)
        return FakeResult(pending_ids)

    def commit(self) -> None:
        self.commits += 1

    def close(self) -> None:
        self.closed = True

    def get(self, _model, identifier):
        return self.rows.get(identifier)

    def add(self, item) -> None:
        self.rows[item.id] = item


class FakeVectorSession:
    def __init__(self) -> None:
        self.added: list[object] = []

    def add(self, item) -> None:
        self.added.append(item)


def test_vector_sync_session_factory_is_reused_per_worker_process(monkeypatch) -> None:
    first_factory = SimpleNamespace(kw={"bind": MagicMock()})
    second_factory = SimpleNamespace(kw={"bind": MagicMock()})
    factories = iter([first_factory, second_factory])
    build_calls: list[int] = []

    vector_sync_tasks.dispose_vector_sync_session_factory()
    monkeypatch.setattr(vector_sync_tasks.os, "getpid", lambda: 101)
    monkeypatch.setattr(
        vector_sync_tasks,
        "build_sync_session_factory",
        lambda: build_calls.append(1) or next(factories),
    )

    assert vector_sync_tasks.build_vector_sync_session_factory() is first_factory
    assert vector_sync_tasks.build_vector_sync_session_factory() is first_factory
    assert len(build_calls) == 1

    monkeypatch.setattr(vector_sync_tasks.os, "getpid", lambda: 202)
    assert vector_sync_tasks.build_vector_sync_session_factory() is second_factory
    assert len(build_calls) == 2
    first_factory.kw["bind"].dispose.assert_called_once_with()

    vector_sync_tasks.dispose_vector_sync_session_factory()
    second_factory.kw["bind"].dispose.assert_called_once_with()


def make_memory(*, tenant_id: uuid.UUID, proxy_user_id: uuid.UUID, content: str = "User prefers Python") -> Memory:
    proxy_user = ProxyUser(
        id=proxy_user_id,
        tenant_id=tenant_id,
        external_user_id="ext-1",
        external_user_id_hash="hash-1",
        memory_count=1,
        metadata_json={},
        is_blocked=False,
    )
    memory = Memory(
        id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        proxy_user_id=proxy_user_id,
        content=content,
        category=MemoryCategory.preference,
        importance_score=7.0,
        confidence_score=0.9,
        embedding_id=str(uuid.uuid4()),
        embedding_model_id="gemini-embedding-001-v1",
        source_conversation_id=uuid.uuid4(),
        previous_version_id=None,
        expires_at=None,
        metadata_json={},
        is_archived=False,
    )
    memory.proxy_user = proxy_user
    return memory


def test_conflict_resolver_enqueues_outbox_rows_instead_of_direct_qdrant_writes() -> None:
    existing = make_memory(tenant_id=uuid.uuid4(), proxy_user_id=uuid.uuid4(), content="User prefers Python")
    session = FakeVectorSession()
    session.memories = {str(existing.id): existing}
    session.flush = lambda: None
    session.commit = lambda: None
    session.get = lambda _model, memory_id: session.memories.get(str(memory_id))
    original_add = session.add

    def add_with_memory_tracking(item):
        original_add(item)
        if isinstance(item, Memory):
            session.memories[str(item.id)] = item

    session.add = add_with_memory_tracking

    qdrant = SimpleNamespace(
        search_memories=lambda **_kwargs: [SimpleNamespace(id=str(existing.id), score=0.99, payload={"memory_id": str(existing.id)})]
    )
    resolver = ConflictResolver(
        session=session,
        qdrant_service=qdrant,
        embedder=lambda _text: [0.1, 0.2, 0.3],
        client=SimpleNamespace(
            models=SimpleNamespace(
                generate_content=lambda **_kwargs: SimpleNamespace(
                    text='{"action":"UPDATE","reasoning":"update"}'
                )
            )
        ),
        default_source_conversation_id=uuid.uuid4(),
    )

    stored = resolver.check_and_store(
        [
            ExtractedMemory(
                content="User switched to Go",
                category="preference",
                importance_score=8.0,
                confidence=0.95,
                expiry="permanent",
                reasoning="Updated preference",
            )
        ],
        user_id=str(uuid.uuid4()),
        tenant_id=str(existing.proxy_user.tenant_id),
        proxy_user_id=str(existing.proxy_user_id),
        auto_commit=False,
    )

    outbox_rows = [item for item in session.added if isinstance(item, VectorSyncOutbox)]
    assert len(stored) == 1
    assert [row.operation for row in outbox_rows] == [VectorSyncOperation.archive, VectorSyncOperation.upsert]


def test_run_outbox_cycle_batches_upserts_and_marks_done(monkeypatch) -> None:
    row = VectorSyncOutbox(
        id=uuid.uuid4(),
        operation=VectorSyncOperation.upsert,
        memory_id=uuid.uuid4(),
        embedding=[0.1, 0.2],
        payload={"memory_id": "mem-1"},
        status=VectorSyncStatus.pending,
        attempts=0,
    )
    session = FakeOutboxSession([row])
    claimed_rows: list[VectorSyncOutbox] = []
    done_rows: list[VectorSyncOutbox] = []

    monkeypatch.setattr(vector_sync_tasks, "_claim_outbox_row", lambda _session, _id: claimed_rows.append(row) or row)
    monkeypatch.setattr(vector_sync_tasks, "_mark_rows_done", lambda _session, rows: done_rows.extend(rows))
    monkeypatch.setattr(vector_sync_tasks, "_mark_rows_failed_or_pending", lambda _session, rows, _err: 0)

    qdrant = SimpleNamespace(
        COLLECTION_NAME="memories",
        client=SimpleNamespace(upsert=lambda **_kwargs: True),
        breaker=SimpleNamespace(call_sync=lambda fn, *args, **kwargs: fn(*args, **kwargs)),
    )
    result = vector_sync_tasks.run_outbox_cycle(
        session_factory=lambda: session,
        qdrant_service=qdrant,
    )

    assert result == {"claimed": 1, "done": 1, "failed": 0}
    assert claimed_rows == [row]
    assert done_rows == [row]
    assert session.closed is True


def test_run_outbox_cycle_processes_upserts_in_safe_chunks(monkeypatch) -> None:
    rows = [
        VectorSyncOutbox(
            id=uuid.uuid4(),
            operation=VectorSyncOperation.upsert,
            memory_id=uuid.uuid4(),
            embedding=[0.1, 0.2],
            payload={"memory_id": f"mem-{index}"},
            status=VectorSyncStatus.pending,
            attempts=0,
        )
        for index in range(12)
    ]
    session = FakeOutboxSession(rows)
    done_rows: list[VectorSyncOutbox] = []
    chunk_sizes: list[int] = []

    def claim(_session, outbox_id):
        row = session.rows[outbox_id]
        row.status = VectorSyncStatus.processing
        row.attempts += 1
        return row

    monkeypatch.setattr(vector_sync_tasks, "_claim_outbox_row", claim)
    monkeypatch.setattr(vector_sync_tasks, "_mark_rows_done", lambda _session, chunk: done_rows.extend(chunk))
    monkeypatch.setattr(vector_sync_tasks, "_mark_rows_failed_or_pending", lambda _session, rows, _err: 0)

    def upsert(**kwargs):
        chunk_sizes.append(len(kwargs["points"]))
        return True

    qdrant = SimpleNamespace(
        COLLECTION_NAME="memories",
        client=SimpleNamespace(upsert=upsert),
        breaker=SimpleNamespace(call_sync=lambda fn, *args, **kwargs: fn(*args, **kwargs)),
    )

    result = vector_sync_tasks.run_outbox_cycle(
        session_factory=lambda: session,
        qdrant_service=qdrant,
        batch_size=12,
    )

    assert result == {"claimed": 12, "done": 12, "failed": 0}
    assert chunk_sizes == [10, 2]
    assert len(done_rows) == 12


def test_run_outbox_cycle_marks_failed_after_three_attempts(monkeypatch) -> None:
    row = VectorSyncOutbox(
        id=uuid.uuid4(),
        operation=VectorSyncOperation.upsert,
        memory_id=uuid.uuid4(),
        embedding=[0.1, 0.2],
        payload={"memory_id": "mem-1"},
        status=VectorSyncStatus.pending,
        attempts=2,
    )
    session = FakeOutboxSession([row])

    def claim(_session, _id):
        row.status = VectorSyncStatus.processing
        row.attempts = 3
        return row

    def mark_failed(_session, rows, error_message):
        for item in rows:
            item.status = VectorSyncStatus.failed
            item.last_error = error_message
        return len(rows)

    monkeypatch.setattr(vector_sync_tasks, "_claim_outbox_row", claim)
    monkeypatch.setattr(vector_sync_tasks, "_mark_rows_done", lambda _session, rows: None)
    monkeypatch.setattr(vector_sync_tasks, "_mark_rows_failed_or_pending", mark_failed)

    qdrant = SimpleNamespace(
        COLLECTION_NAME="memories",
        client=SimpleNamespace(upsert=lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("qdrant down"))),
        breaker=SimpleNamespace(call_sync=lambda fn, *args, **kwargs: fn(*args, **kwargs)),
    )
    result = vector_sync_tasks.run_outbox_cycle(
        session_factory=lambda: session,
        qdrant_service=qdrant,
    )

    assert result == {"claimed": 1, "done": 0, "failed": 1}
    assert row.status == VectorSyncStatus.failed
    assert row.last_error == "qdrant down"


def test_reconciliation_enqueues_missing_and_deletes_orphans(monkeypatch) -> None:
    tenant_id = uuid.uuid4()
    proxy_user_id = uuid.uuid4()
    memory = make_memory(tenant_id=tenant_id, proxy_user_id=proxy_user_id)
    session = FakeVectorSession()
    summary = {"checked": 0, "missing_in_qdrant": 0, "orphan_in_qdrant": 0, "repaired": 0}

    embedding_service = SimpleNamespace(
        embed_sync=lambda _text, model_id=None: EmbeddingResult(
            vector=[0.5, 0.6],
            model_id=model_id or "gemini-embedding-001-v1",
            dimensions=1536,
            qdrant_collection="memories",
        )
    )

    repaired_missing = reconciliation_tasks._enqueue_missing_repair(session, [memory], embedding_service)
    assert repaired_missing == 1
    assert any(isinstance(item, VectorSyncOutbox) and item.operation == VectorSyncOperation.upsert for item in session.added)

    class FakeReconciliationSession:
        def execute(self, _statement):
            return FakeResult([])

    deleted = []
    qdrant = SimpleNamespace(
        COLLECTION_NAME="memories",
        client=SimpleNamespace(
            scroll=lambda **_kwargs: ([SimpleNamespace(id="orphan-1")], None),
            delete=lambda **kwargs: deleted.append(kwargs) or True,
        ),
        breaker=SimpleNamespace(call_sync=lambda fn, *args, **kwargs: fn(*args, **kwargs)),
    )

    repaired_orphans = reconciliation_tasks._remove_orphan_vectors(
        FakeReconciliationSession(),
        qdrant,
        summary,
        page_size=100,
    )
    assert repaired_orphans == 1
    assert summary["orphan_in_qdrant"] == 1
    assert deleted[0]["points_selector"].points == ["orphan-1"]
