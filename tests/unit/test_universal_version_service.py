from __future__ import annotations

import uuid
from datetime import UTC
from datetime import datetime

import pytest

from api.db.models import UniversalMemory
from api.db.models import UniversalMemoryVersion
from api.services.version_service import VersionService


class FakeScalarResult:
    def __init__(self, items):
        self._items = list(items)

    def all(self):
        return list(self._items)

    def scalar_one(self):
        return self._items[0] if self._items else 0


class FakeExecuteResult:
    def __init__(self, items):
        self._items = list(items)

    def scalars(self):
        return FakeScalarResult(self._items)

    def scalar_one(self):
        return self._items[0] if self._items else 0


class FakeSyncSession:
    def __init__(self):
        self.added = []

    def execute(self, _statement):
        versions = [item for item in self.added if isinstance(item, UniversalMemoryVersion)]
        max_version = max([version.version_number for version in versions], default=0)
        return FakeExecuteResult([max_version])

    def add(self, item):
        self.added.append(item)


class FakeAsyncSession:
    def __init__(self, execute_results, get_result=None):
        self.execute_results = list(execute_results)
        self.get_result = get_result
        self.added = []

    async def execute(self, _statement):
        return FakeExecuteResult(self.execute_results.pop(0))

    async def get(self, _model, _id):
        return self.get_result

    def add(self, item):
        self.added.append(item)


def make_universal_memory(user_uui_id: uuid.UUID | None = None) -> UniversalMemory:
    now = datetime.now(UTC)
    return UniversalMemory(
        id=uuid.uuid4(),
        user_uui_id=user_uui_id or uuid.uuid4(),
        source_agent_id=uuid.uuid4(),
        content="User is now in Class 11.",
        category="fact",
        importance_score=7.0,
        confidence=0.9,
        embedding_id=str(uuid.uuid4()),
        created_at=now,
        last_accessed_at=now,
        is_archived=False,
        is_flagged=False,
        metadata_json={},
    )


def make_universal_version(memory: UniversalMemory, number: int) -> UniversalMemoryVersion:
    return UniversalMemoryVersion(
        id=uuid.uuid4(),
        universal_memory_id=memory.id,
        version_number=number,
        content=memory.content,
        category=memory.category,
        importance_score=memory.importance_score,
        confidence=memory.confidence,
        change_type="created",
        change_reason="test",
        changed_by="user",
        created_at=datetime.now(UTC),
    )


def test_record_universal_version_sync_uses_separate_table() -> None:
    memory = make_universal_memory()
    session = FakeSyncSession()

    first = VersionService(session).record_universal_version_sync(
        memory,
        "created",
        "Created from test",
        "user",
    )
    memory.content = "User corrected this memory."
    second = VersionService(session).record_universal_version_sync(
        memory,
        "user_corrected",
        "User corrected to: User corrected this memory.",
        "user",
    )

    assert first.version_number == 1
    assert second.version_number == 2
    assert second.content == "User corrected this memory."
    assert session.added == [first, second]


@pytest.mark.asyncio
async def test_record_universal_version_async_validates_change_type() -> None:
    session = FakeAsyncSession([[0]])

    with pytest.raises(ValueError):
        await VersionService(session).record_universal_version(
            make_universal_memory(),
            "manual_edit",
            "wrong table change type",
        )


@pytest.mark.asyncio
async def test_get_universal_history_verifies_user_ownership() -> None:
    user_id = uuid.uuid4()
    memory = make_universal_memory(user_id)
    versions = [make_universal_version(memory, 1), make_universal_version(memory, 2)]
    session = FakeAsyncSession([versions], get_result=memory)

    history = await VersionService(session).get_universal_history(str(memory.id), str(user_id))

    assert [version.version_number for version in history] == [1, 2]


@pytest.mark.asyncio
async def test_get_universal_history_blocks_other_users() -> None:
    memory = make_universal_memory(uuid.uuid4())
    session = FakeAsyncSession([], get_result=memory)

    with pytest.raises(PermissionError):
        await VersionService(session).get_universal_history(str(memory.id), str(uuid.uuid4()))
