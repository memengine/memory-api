from __future__ import annotations

import uuid
from datetime import UTC
from datetime import datetime

import pytest

from api.db.models import Memory
from api.db.models import MemoryCategory
from api.db.models import MemoryVersion
from api.db.models import ProxyUser
from api.services.embedding_service import DEFAULT_ACTIVE_MODEL_ID
from api.services.version_service import VersionService


class FakeScalarResult:
    def __init__(self, items):
        self._items = list(items)

    def all(self):
        return list(self._items)

    def scalar_one_or_none(self):
        return self._items[0] if self._items else None

    def scalar_one(self):
        return self._items[0] if self._items else 0


class FakeExecuteResult:
    def __init__(self, items):
        self._items = list(items)

    def scalars(self):
        return FakeScalarResult(self._items)

    def scalar_one_or_none(self):
        return self._items[0] if self._items else None

    def scalar_one(self):
        return self._items[0] if self._items else 0


class FakeSyncSession:
    def __init__(self):
        self.added = []

    def execute(self, _statement):
        versions = [item for item in self.added if isinstance(item, MemoryVersion)]
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


def make_memory(*, proxy_user_id: uuid.UUID | None = None) -> Memory:
    now = datetime.now(UTC)
    return Memory(
        id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        proxy_user_id=proxy_user_id or uuid.uuid4(),
        agent_id=None,
        content="User prefers concise technical explanations",
        category=MemoryCategory.preference,
        importance_score=8.0,
        confidence_score=0.92,
        embedding_id=str(uuid.uuid4()),
        embedding_model_id=DEFAULT_ACTIVE_MODEL_ID,
        source_conversation_id=uuid.uuid4(),
        previous_version_id=None,
        expires_at=None,
        metadata_json={},
        is_archived=False,
        created_at=now,
        updated_at=now,
    )


def make_version(memory: Memory, number: int, change_type: str = "created") -> MemoryVersion:
    return MemoryVersion(
        id=uuid.uuid4(),
        memory_id=memory.id,
        version_number=number,
        content=memory.content,
        category=memory.category.value,
        importance_score=memory.importance_score,
        confidence=memory.confidence_score,
        change_type=change_type,
        change_reason="test change",
        changed_by="system",
        created_at=datetime.now(UTC),
    )


def test_record_version_uses_next_version_number_and_current_memory_state() -> None:
    memory = make_memory()
    memory.content_envelope = {"version": 1, "ciphertext": "first"}
    session = FakeSyncSession()
    first = VersionService(session).record_version(memory, "created", "Extracted from conversation")

    memory.content = "User prefers short Python examples"
    memory.content_envelope = {"version": 1, "ciphertext": "second"}
    memory.importance_score = 9.0
    second = VersionService(session).record_version(memory, "manual_edit", "Edited by tenant admin", "user")

    assert first.version_number == 1
    assert first.content == "User prefers concise technical explanations"
    assert first.content_envelope == {"version": 1, "ciphertext": "first"}
    assert second.version_number == 2
    assert second.content == "User prefers short Python examples"
    assert second.content_envelope == {"version": 1, "ciphertext": "second"}
    assert second.importance_score == 9.0
    assert session.added == [first, second]


def test_record_version_rejects_invalid_change_type() -> None:
    with pytest.raises(ValueError):
        VersionService(FakeSyncSession()).record_version(make_memory(), "bad_change")


@pytest.mark.asyncio
async def test_get_history_verifies_tenant_before_returning_versions() -> None:
    tenant_id = uuid.uuid4()
    memory = make_memory()
    versions = [make_version(memory, 1), make_version(memory, 2, "manual_edit")]
    session = FakeAsyncSession([[memory], versions])

    history = await VersionService(session).get_history(str(memory.id), str(tenant_id))

    assert [version.version_number for version in history] == [1, 2]


@pytest.mark.asyncio
async def test_get_history_blocks_cross_tenant_access() -> None:
    session = FakeAsyncSession([[]])

    with pytest.raises(PermissionError):
        await VersionService(session).get_history(str(uuid.uuid4()), str(uuid.uuid4()))


@pytest.mark.asyncio
async def test_user_data_export_includes_archived_memories_and_versions() -> None:
    tenant_id = uuid.uuid4()
    proxy_user_id = uuid.uuid4()
    proxy_user = ProxyUser(
        id=proxy_user_id,
        tenant_id=tenant_id,
        external_user_id="student-1",
        external_user_id_hash="hash",
    )
    active_memory = make_memory(proxy_user_id=proxy_user_id)
    archived_memory = make_memory(proxy_user_id=proxy_user_id)
    archived_memory.is_archived = True
    versions = [
        make_version(active_memory, 1),
        make_version(archived_memory, 1),
        make_version(archived_memory, 2, "archived"),
    ]
    session = FakeAsyncSession([[active_memory, archived_memory], versions], get_result=proxy_user)

    export = await VersionService(session).get_user_data_export(str(proxy_user_id), str(tenant_id))

    assert export.tenant_id == str(tenant_id)
    assert len(export.memories) == 2
    assert [memory["is_archived"] for memory in export.memories] == [False, True]
    assert len(export.memories[0]["versions"]) == 1
    assert len(export.memories[1]["versions"]) == 2
