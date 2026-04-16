from __future__ import annotations

from unittest.mock import AsyncMock
from unittest.mock import MagicMock
from uuid import uuid4

import pytest

from api.db.models import Memory
from api.db.models import MemoryCategory
from api.db.models import ProxyUser
from api.db.models import QuotaMode
from api.services.memory_service import MemoryService
from api.services.quota_manager import QuotaEnvelope


class FakeQuotaManager:
    def __init__(self, envelope: QuotaEnvelope) -> None:
        self.envelope = envelope
        self.get_quota_envelope = AsyncMock(return_value=envelope)


class FakeDispatchTask:
    def __init__(self) -> None:
        self.calls = []

    def __call__(self, task_name, *args, **kwargs):
        self.calls.append((task_name, args, kwargs))
        return None


class FakeScalarResult:
    def __init__(self, items) -> None:
        self._items = list(items)

    def all(self):
        return list(self._items)

    def scalar_one_or_none(self):
        if not self._items:
            return None
        return self._items[0]


class FakeExecuteResult:
    def __init__(self, items) -> None:
        self._items = list(items)

    def scalars(self):
        return FakeScalarResult(self._items)

    def scalar_one_or_none(self):
        if not self._items:
            return None
        return self._items[0]


@pytest.mark.asyncio
async def test_queue_memory_add_returns_blocked_when_quota_mode_is_blocked() -> None:
    quota_manager = FakeQuotaManager(
        QuotaEnvelope(mode=QuotaMode.blocked, budget_remaining_pct=0.0, reset_at=None)
    )
    cache_service = MagicMock()
    cache_service.get_idempotent_response = AsyncMock(return_value=None)
    cache_service.set_job_status = AsyncMock()
    cache_service.set_idempotent_response = AsyncMock()

    service = MemoryService(
        session=MagicMock(),
        cache_service=cache_service,
        qdrant_service=MagicMock(),
        quota_manager=quota_manager,
    )

    result = await service.queue_memory_add(
        requested_user_id="user_123",
        authenticated_user_id=None,
        agent_id=None,
        messages=[{"role": "user", "content": "hello"}],
        metadata={},
        idempotency_key=None,
        tenant_id="tenant_123",
        external_user_id="external_123",
    )

    assert result["status"] == "blocked"
    assert result["job_id"] is None
    assert result["budget_remaining_pct"] == 0.0
    cache_service.set_job_status.assert_not_awaited()


@pytest.mark.asyncio
async def test_queue_memory_add_returns_passthrough_when_quota_mode_is_passthrough() -> None:
    quota_manager = FakeQuotaManager(
        QuotaEnvelope(mode=QuotaMode.passthrough, budget_remaining_pct=0.22, reset_at=None)
    )
    cache_service = MagicMock()
    cache_service.get_idempotent_response = AsyncMock(return_value=None)
    cache_service.set_job_status = AsyncMock()
    cache_service.set_idempotent_response = AsyncMock()

    service = MemoryService(
        session=MagicMock(),
        cache_service=cache_service,
        qdrant_service=MagicMock(),
        quota_manager=quota_manager,
    )

    result = await service.queue_memory_add(
        requested_user_id="user_123",
        authenticated_user_id=None,
        agent_id=None,
        messages=[{"role": "user", "content": "hello"}],
        metadata={},
        idempotency_key=None,
        tenant_id="tenant_123",
        external_user_id="external_123",
    )

    assert result["status"] == "passthrough"
    assert result["job_id"] is None
    assert result["budget_remaining_pct"] == 0.22
    cache_service.set_job_status.assert_not_awaited()


@pytest.mark.asyncio
async def test_queue_memory_add_dispatches_extraction_task_when_queued() -> None:
    quota_manager = FakeQuotaManager(
        QuotaEnvelope(mode=QuotaMode.full, budget_remaining_pct=0.88, reset_at=None)
    )
    cache_service = MagicMock()
    cache_service.get_idempotent_response = AsyncMock(return_value=None)
    cache_service.set_job_status = AsyncMock()
    cache_service.set_idempotent_response = AsyncMock()
    dispatch_task = FakeDispatchTask()

    service = MemoryService(
        session=MagicMock(),
        cache_service=cache_service,
        qdrant_service=MagicMock(),
        quota_manager=quota_manager,
        dispatch_task=dispatch_task,
    )

    result = await service.queue_memory_add(
        requested_user_id=None,
        authenticated_user_id=None,
        agent_id=None,
        messages=[{"role": "user", "content": "hello"}],
        metadata={"session_id": "sess_1"},
        idempotency_key=None,
        tenant_id=None,
        external_user_id=None,
    )

    assert result["status"] == "queued"
    assert result["job_id"] is not None
    cache_service.set_job_status.assert_awaited_once()
    assert dispatch_task.calls[0][0] == "api.tasks.extraction_tasks.process_extraction_job"
    assert dispatch_task.calls[0][2]["args"][0]["job_id"] == result["job_id"]
    assert dispatch_task.calls[0][2]["queue"] is None


@pytest.mark.asyncio
async def test_list_memories_uses_tenant_scope_without_user_lookup() -> None:
    tenant_id = str(uuid4())
    proxy_user_id = uuid4()
    memory = Memory(
        id=uuid4(),
        user_id=uuid4(),
        proxy_user_id=proxy_user_id,
        agent_id=None,
        content="Tenant scoped memory",
        category=MemoryCategory.preference,
        importance_score=0.8,
        confidence_score=0.9,
        embedding_id="emb-1",
        embedding_model_id="openai-text-embedding-3-small-v1",
        source_conversation_id=uuid4(),
        metadata_json={},
        is_archived=False,
    )

    session = MagicMock()
    session.execute = AsyncMock(return_value=FakeExecuteResult([memory]))
    cache_service = MagicMock()
    service = MemoryService(
        session=session,
        cache_service=cache_service,
        qdrant_service=MagicMock(),
        quota_manager=MagicMock(),
    )

    memories, next_cursor, total = await service.list_memories(
        requested_user_id=None,
        authenticated_user_id=None,
        tenant_id=tenant_id,
        cursor=None,
        limit=10,
        categories=[],
        agent_id=None,
        external_user_id="AVIRAL",
    )

    assert [item.id for item in memories] == [memory.id]
    assert next_cursor is None
    assert total == 1
    session.execute.assert_awaited_once()


@pytest.mark.asyncio
async def test_delete_memory_uses_tenant_scope_when_tenant_id_present() -> None:
    tenant_id = str(uuid4())
    memory = Memory(
        id=uuid4(),
        user_id=uuid4(),
        proxy_user_id=uuid4(),
        agent_id=None,
        content="Tenant scoped memory",
        category=MemoryCategory.preference,
        importance_score=0.8,
        confidence_score=0.9,
        embedding_id="emb-1",
        embedding_model_id="openai-text-embedding-3-small-v1",
        source_conversation_id=uuid4(),
        metadata_json={},
        is_archived=False,
    )

    session = MagicMock()
    session.execute = AsyncMock(return_value=FakeExecuteResult([memory]))
    session.commit = AsyncMock()
    session.get = AsyncMock(return_value=None)
    cache_service = MagicMock()
    cache_service.invalidate_user_cache = AsyncMock()
    service = MemoryService(
        session=session,
        cache_service=cache_service,
        qdrant_service=MagicMock(),
        quota_manager=MagicMock(),
    )
    service._embedding_collection_for_memory = AsyncMock(return_value="memories_v1")

    deleted = await service.delete_memory(
        authenticated_user_id=None,
        tenant_id=tenant_id,
        memory_id=str(memory.id),
        hard_delete=False,
    )

    assert deleted is True
    assert memory.is_archived is True
    session.commit.assert_awaited_once()
    cache_service.invalidate_user_cache.assert_awaited_once()
