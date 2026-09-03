from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest

from api.db.models import VectorSyncOperation, VectorSyncOutbox
from api.services.uui_service import UUIService
from api.tasks import vector_sync_tasks


class _ScalarRows:
    def __init__(self, rows: list[object]) -> None:
        self._rows = rows

    def all(self) -> list[object]:
        return list(self._rows)


class _ExecuteResult:
    def __init__(self, rows: list[object]) -> None:
        self._rows = rows

    def scalars(self) -> _ScalarRows:
        return _ScalarRows(self._rows)


class _DeletionSession:
    def __init__(self, memory_ids: list[uuid.UUID]) -> None:
        self.memory_ids = memory_ids
        self.added: list[object] = []
        self.deleted: list[object] = []
        self.committed = False

    def add(self, item: object) -> None:
        self.added.append(item)

    async def execute(self, statement):
        statement_text = str(statement).lower()
        if "universal_memories.id" in statement_text:
            return _ExecuteResult(self.memory_ids)
        if "permission_grants.agent_id" in statement_text:
            return _ExecuteResult([])
        raise AssertionError(f"Unexpected statement: {statement}")

    async def delete(self, item: object) -> None:
        self.deleted.append(item)

    async def commit(self) -> None:
        self.committed = True


@pytest.mark.asyncio
async def test_universal_user_deletion_is_durable_when_qdrant_is_unavailable(monkeypatch) -> None:
    memory_ids = [uuid.uuid4(), uuid.uuid4()]
    session = _DeletionSession(memory_ids)
    user = SimpleNamespace(id=uuid.uuid4(), uui_token="uui_test")
    service = UUIService(session=session, cache_service=None, qdrant_service=None)

    async def resolve_by_token(_token: str):
        return user

    monkeypatch.setattr(service, "resolve_by_token", resolve_by_token)

    deleted, count = await service.delete_user_data(uui_token=user.uui_token)

    assert (deleted, count) == (True, 2)
    assert session.deleted == [user]
    assert session.committed is True
    outbox_rows = [item for item in session.added if isinstance(item, VectorSyncOutbox)]
    assert [row.memory_id for row in outbox_rows] == memory_ids
    assert all(row.operation == VectorSyncOperation.delete for row in outbox_rows)
    assert all(row.payload == {"qdrant_collection": "universal_memories", "privacy_delete": True} for row in outbox_rows)


def test_privacy_delete_outbox_row_remains_pending_after_normal_retry_limit() -> None:
    row = VectorSyncOutbox(
        id=uuid.uuid4(),
        operation=VectorSyncOperation.delete,
        memory_id=uuid.uuid4(),
        embedding=None,
        payload={"qdrant_collection": "universal_memories", "privacy_delete": True},
        attempts=3,
    )
    session = SimpleNamespace(
        get=lambda _model, identifier: row if identifier == row.id else None,
        add=lambda _item: None,
    )

    failed = vector_sync_tasks._mark_rows_failed_or_pending(session, [row], "qdrant unavailable")

    assert failed == 0
    assert row.status.value == "pending"
    assert row.last_error == "qdrant unavailable"
