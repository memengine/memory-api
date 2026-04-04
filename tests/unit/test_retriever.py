from __future__ import annotations

import uuid
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
from unittest.mock import MagicMock

import pytest

from api.db.models import Memory
from api.db.models import MemoryCategory
from api.db.models import QuotaMode
from api.services.embedding_service import DEFAULT_ACTIVE_MODEL_ID
from api.services.embedding_service import EmbeddingResult
from api.services.retriever import MemoryResult
from api.services.retriever import RetrieverService


class FakeExecuteResult:
    def __init__(self, scalar_value=None, items=None) -> None:
        self._scalar_value = scalar_value
        self._items = list(items or [])

    def scalar_one(self):
        return self._scalar_value

    def scalar_one_or_none(self):
        return self._scalar_value

    def scalars(self):
        return self

    def all(self):
        return list(self._items)


class FakeAioModels:
    def __init__(self, embedding):
        self.embedding = embedding
        self.embed_content = AsyncMock(
            return_value=SimpleNamespace(
                embeddings=[SimpleNamespace(values=self.embedding)]
            )
        )


class FakeGenAIClient:
    def __init__(self, embedding):
        self.aio = SimpleNamespace(models=FakeAioModels(embedding))


class FakeQuotaManager:
    def __init__(self, mode: QuotaMode = QuotaMode.full):
        self.mode = mode
        self.get_mode = AsyncMock(return_value=mode)


class FailingQuotaManager:
    def __init__(self, error: Exception | None = None):
        self.error = error or RuntimeError("quota failure")
        self.get_mode = AsyncMock(side_effect=self.error)


def make_memory(
    *,
    content: str,
    category: MemoryCategory = MemoryCategory.fact,
    importance_score: float = 5.0,
    last_accessed_at: datetime | None = None,
    agent_id: uuid.UUID | None = None,
) -> Memory:
    return Memory(
        id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        proxy_user_id=uuid.uuid4(),
        agent_id=agent_id,
        content=content,
        category=category,
        importance_score=importance_score,
        confidence_score=0.9,
        embedding_id=str(uuid.uuid4()),
        embedding_model_id=DEFAULT_ACTIVE_MODEL_ID,
        source_conversation_id=uuid.uuid4(),
        previous_version_id=None,
        expires_at=None,
        metadata_json={},
        access_count=0,
        last_accessed_at=last_accessed_at or datetime.now(UTC),
        is_archived=False,
    )


def make_point(memory: Memory, score: float) -> SimpleNamespace:
    return SimpleNamespace(
        id=str(memory.id),
        score=score,
        payload={"memory_id": str(memory.id)},
    )


def make_embedding_model(
    model_id: str = DEFAULT_ACTIVE_MODEL_ID,
    qdrant_collection: str = "memories",
) -> SimpleNamespace:
    return SimpleNamespace(
        id=model_id,
        provider=SimpleNamespace(value="gemini"),
        model_name="gemini-embedding-001",
        dimensions=1536,
        qdrant_collection=qdrant_collection,
        is_active=True,
        deprecated_at=None,
        created_at=SimpleNamespace(isoformat=lambda: "2026-04-01T00:00:00+00:00"),
    )


class FakeEmbeddingService:
    async def embed(self, text: str, model_id: str | None = None) -> EmbeddingResult:
        return EmbeddingResult(
            vector=[0.1, 0.2],
            model_id=model_id or DEFAULT_ACTIVE_MODEL_ID,
            dimensions=1536,
            qdrant_collection="memories",
        )

    async def get_active_model(self):
        return SimpleNamespace(id=DEFAULT_ACTIVE_MODEL_ID)


@pytest.mark.asyncio
async def test_retrieve_returns_cached_results_immediately(monkeypatch) -> None:
    cache_service = MagicMock()
    cache_service.get_hot_memories = AsyncMock(
        return_value=[
            {
                "id": "memory-1",
                "content": "User prefers concise answers",
                "category": "preference",
                "importance_score": 8.0,
                "confidence_score": 0.9,
                "semantic_score": 0.9,
                "recency_score": 1.0,
                "final_score": 0.94,
                "agent_id": None,
                "previous_version_id": None,
                "last_accessed_at": datetime.now(UTC).isoformat(),
                "_cache_context": "pricing|| |10".replace(" ", ""),
            }
        ]
    )
    cache_service.set_hot_memories = AsyncMock()
    session = MagicMock()
    session.execute = AsyncMock()
    qdrant_service = MagicMock()
    task_mock = MagicMock()
    monkeypatch.setattr("api.services.retriever.update_memory_accesses", task_mock)

    service = RetrieverService(
        session=session,
        qdrant_service=qdrant_service,
        cache_service=cache_service,
        quota_manager=FakeQuotaManager(),
        embedding_service=FakeEmbeddingService(),
        client=FakeGenAIClient([0.1, 0.2]),
    )

    results = await service.retrieve(query="pricing", user_id=str(uuid.uuid4()))

    assert len(results) == 1
    assert results[0].content == "User prefers concise answers"
    session.execute.assert_not_awaited()
    qdrant_service.search_memories.assert_not_called()
    task_mock.delay.assert_called_once_with(["memory-1"])


@pytest.mark.asyncio
async def test_cold_start_returns_all_memories_without_embedding_or_qdrant(monkeypatch) -> None:
    user_id = str(uuid.uuid4())
    cold_memories = [
        make_memory(content="User works in healthcare", category=MemoryCategory.fact, importance_score=6.0),
        make_memory(content="User is an engineer", category=MemoryCategory.fact, importance_score=7.0),
    ]
    session = MagicMock()
    session.execute = AsyncMock(
        side_effect=[
            FakeExecuteResult(scalar_value=2),
            FakeExecuteResult(items=cold_memories),
        ]
    )
    cache_service = MagicMock()
    cache_service.get_hot_memories = AsyncMock(return_value=None)
    cache_service.set_hot_memories = AsyncMock()
    qdrant_service = MagicMock()
    task_mock = MagicMock()
    monkeypatch.setattr("api.services.retriever.update_memory_accesses", task_mock)
    client = FakeGenAIClient([0.1, 0.2])

    service = RetrieverService(
        session=session,
        qdrant_service=qdrant_service,
        cache_service=cache_service,
        quota_manager=FakeQuotaManager(),
        client=client,
    )

    results = await service.retrieve(query="any query", user_id=user_id, limit=10)

    assert len(results) == 2
    assert {result.content for result in results} == {
        "User works in healthcare",
        "User is an engineer",
    }
    client.aio.models.embed_content.assert_not_awaited()
    qdrant_service.search_memories.assert_not_called()
    task_mock.delay.assert_called_once()


@pytest.mark.asyncio
async def test_retrieve_applies_hybrid_scoring_and_returns_best_match(monkeypatch) -> None:
    user_id = str(uuid.uuid4())
    now = datetime.now(UTC)
    stronger_memory = make_memory(
        content="User's launch goal",
        category=MemoryCategory.goal,
        importance_score=10.0,
        last_accessed_at=now - timedelta(days=2),
    )
    weaker_memory = make_memory(
        content="User's old note",
        category=MemoryCategory.fact,
        importance_score=2.0,
        last_accessed_at=now - timedelta(days=45),
    )
    session = MagicMock()
    session.execute = AsyncMock(
        side_effect=[
            FakeExecuteResult(scalar_value=10),
            FakeExecuteResult(scalar_value=make_embedding_model()),
            FakeExecuteResult(items=[stronger_memory, weaker_memory]),
        ]
    )
    cache_service = MagicMock()
    cache_service.get_hot_memories = AsyncMock(return_value=None)
    cache_service.set_hot_memories = AsyncMock()
    qdrant_service = MagicMock()
    qdrant_service.search_memories.return_value = [
        make_point(stronger_memory, 0.60),
        make_point(weaker_memory, 0.90),
    ]
    task_mock = MagicMock()
    monkeypatch.setattr("api.services.retriever.update_memory_accesses", task_mock)

    service = RetrieverService(
        session=session,
        qdrant_service=qdrant_service,
        cache_service=cache_service,
        quota_manager=FakeQuotaManager(),
        embedding_service=FakeEmbeddingService(),
        client=FakeGenAIClient([0.1, 0.2]),
    )

    results = await service.retrieve(query="launch", user_id=user_id, limit=2)

    assert len(results) == 2
    assert results[0].id == str(stronger_memory.id)
    assert results[0].final_score > results[1].final_score
    task_mock.delay.assert_called_once_with([str(stronger_memory.id), str(weaker_memory.id)])


@pytest.mark.asyncio
async def test_retrieve_deduplicates_near_identical_memories(monkeypatch) -> None:
    user_id = str(uuid.uuid4())
    memory_a = make_memory(content="User uses FastAPI", category=MemoryCategory.expertise, importance_score=7.0)
    memory_b = make_memory(content="User uses FastAPI.", category=MemoryCategory.expertise, importance_score=6.0)
    session = MagicMock()
    session.execute = AsyncMock(
        side_effect=[
            FakeExecuteResult(scalar_value=8),
            FakeExecuteResult(scalar_value=make_embedding_model()),
            FakeExecuteResult(items=[memory_a, memory_b]),
        ]
    )
    cache_service = MagicMock()
    cache_service.get_hot_memories = AsyncMock(return_value=None)
    cache_service.set_hot_memories = AsyncMock()
    qdrant_service = MagicMock()
    qdrant_service.search_memories.return_value = [
        make_point(memory_a, 0.8),
        make_point(memory_b, 0.79),
    ]
    task_mock = MagicMock()
    monkeypatch.setattr("api.services.retriever.update_memory_accesses", task_mock)

    service = RetrieverService(
        session=session,
        qdrant_service=qdrant_service,
        cache_service=cache_service,
        quota_manager=FakeQuotaManager(),
        embedding_service=FakeEmbeddingService(),
        client=FakeGenAIClient([0.1, 0.2]),
    )

    results = await service.retrieve(query="fastapi", user_id=user_id, limit=10)

    assert len(results) == 1
    assert results[0].id == str(memory_a.id)
    task_mock.delay.assert_called_once_with([str(memory_a.id)])


@pytest.mark.asyncio
async def test_retrieve_uses_multiple_category_filters(monkeypatch) -> None:
    user_id = str(uuid.uuid4())
    preference_memory = make_memory(content="User prefers prose", category=MemoryCategory.preference)
    goal_memory = make_memory(content="User wants to launch soon", category=MemoryCategory.goal)
    session = MagicMock()
    session.execute = AsyncMock(
        side_effect=[
            FakeExecuteResult(scalar_value=9),
            FakeExecuteResult(scalar_value=make_embedding_model()),
            FakeExecuteResult(items=[preference_memory, goal_memory]),
        ]
    )
    cache_service = MagicMock()
    cache_service.get_hot_memories = AsyncMock(return_value=None)
    cache_service.set_hot_memories = AsyncMock()
    qdrant_service = MagicMock()
    qdrant_service.search_memories.side_effect = [
        [make_point(preference_memory, 0.7)],
        [make_point(goal_memory, 0.8)],
    ]
    task_mock = MagicMock()
    monkeypatch.setattr("api.services.retriever.update_memory_accesses", task_mock)

    service = RetrieverService(
        session=session,
        qdrant_service=qdrant_service,
        cache_service=cache_service,
        quota_manager=FakeQuotaManager(),
        embedding_service=FakeEmbeddingService(),
        client=FakeGenAIClient([0.1, 0.2]),
    )

    results = await service.retrieve(
        query="help me",
        user_id=user_id,
        categories=["preference", "goal"],
        limit=10,
    )

    assert len(results) == 2
    assert qdrant_service.search_memories.call_count == 2
    task_mock.delay.assert_called_once()


def test_memory_result_cache_round_trip() -> None:
    payload = {
        "id": "memory-1",
        "content": "User prefers prose",
        "category": "preference",
        "importance_score": 8.0,
        "confidence_score": 0.9,
        "semantic_score": 0.92,
        "recency_score": 1.0,
        "final_score": 0.91,
        "agent_id": None,
        "previous_version_id": None,
        "last_accessed_at": datetime.now(UTC).isoformat(),
    }

    result = RetrieverService._memory_result_from_cache(payload)

    assert isinstance(result, MemoryResult)
    assert result.content == "User prefers prose"


@pytest.mark.asyncio
async def test_retrieve_returns_empty_in_passthrough_mode_without_qdrant(monkeypatch) -> None:
    session = MagicMock()
    session.execute = AsyncMock()
    cache_service = MagicMock()
    cache_service.get_hot_memories = AsyncMock(return_value=None)
    cache_service.set_hot_memories = AsyncMock()
    qdrant_service = MagicMock()
    task_mock = MagicMock()
    monkeypatch.setattr("api.services.retriever.update_memory_accesses", task_mock)

    service = RetrieverService(
        session=session,
        qdrant_service=qdrant_service,
        cache_service=cache_service,
        quota_manager=FakeQuotaManager(QuotaMode.passthrough),
        client=FakeGenAIClient([0.1, 0.2]),
    )

    results = await service.retrieve(
        query="pricing",
        user_id=str(uuid.uuid4()),
        tenant_id=str(uuid.uuid4()),
    )

    assert results == []
    qdrant_service.search_memories.assert_not_called()
    session.execute.assert_not_awaited()
    task_mock.delay.assert_not_called()


@pytest.mark.asyncio
async def test_retrieve_in_degraded_mode_uses_cache_only(monkeypatch) -> None:
    session = MagicMock()
    session.execute = AsyncMock()
    cache_service = MagicMock()
    cache_service.get_hot_memories = AsyncMock(return_value=None)
    cache_service.set_hot_memories = AsyncMock()
    qdrant_service = MagicMock()
    task_mock = MagicMock()
    monkeypatch.setattr("api.services.retriever.update_memory_accesses", task_mock)

    service = RetrieverService(
        session=session,
        qdrant_service=qdrant_service,
        cache_service=cache_service,
        quota_manager=FakeQuotaManager(QuotaMode.degraded_retrieve),
        client=FakeGenAIClient([0.1, 0.2]),
    )

    results = await service.retrieve(
        query="pricing",
        user_id=str(uuid.uuid4()),
        tenant_id=str(uuid.uuid4()),
    )

    assert results == []
    qdrant_service.search_memories.assert_not_called()
    session.execute.assert_not_awaited()
    task_mock.delay.assert_not_called()


@pytest.mark.asyncio
async def test_retrieve_defaults_to_full_mode_when_quota_manager_errors(monkeypatch) -> None:
    user_id = str(uuid.uuid4())
    memory = make_memory(content="User prefers Python", category=MemoryCategory.preference)
    session = MagicMock()
    session.execute = AsyncMock(
        side_effect=[
            FakeExecuteResult(scalar_value=1),
            FakeExecuteResult(items=[memory]),
        ]
    )
    cache_service = MagicMock()
    cache_service.get_hot_memories = AsyncMock(return_value=None)
    cache_service.set_hot_memories = AsyncMock()
    qdrant_service = MagicMock()
    task_mock = MagicMock()
    monkeypatch.setattr("api.services.retriever.update_memory_accesses", task_mock)

    service = RetrieverService(
        session=session,
        qdrant_service=qdrant_service,
        cache_service=cache_service,
        quota_manager=FailingQuotaManager(),
        client=FakeGenAIClient([0.1, 0.2]),
    )

    results = await service.retrieve(
        query="python",
        user_id=user_id,
        tenant_id=str(uuid.uuid4()),
    )

    assert len(results) == 1
    assert service.last_quota_mode == QuotaMode.full.value


@pytest.mark.asyncio
async def test_retrieve_returns_empty_when_gemini_embedding_fails(monkeypatch) -> None:
    session = MagicMock()
    session.execute = AsyncMock(
        side_effect=[
            FakeExecuteResult(scalar_value=10),
            FakeExecuteResult(items=[DEFAULT_ACTIVE_MODEL_ID]),
        ]
    )
    cache_service = MagicMock()
    cache_service.get_hot_memories = AsyncMock(return_value=None)
    cache_service.set_hot_memories = AsyncMock()
    qdrant_service = MagicMock()
    task_mock = MagicMock()
    monkeypatch.setattr("api.services.retriever.update_memory_accesses", task_mock)

    client = FakeGenAIClient([0.1, 0.2])
    client.aio.models.embed_content = AsyncMock(side_effect=RuntimeError("gemini failure"))

    service = RetrieverService(
        session=session,
        qdrant_service=qdrant_service,
        cache_service=cache_service,
        quota_manager=FakeQuotaManager(),
        client=client,
    )

    results = await service.retrieve(
        query="python",
        user_id=str(uuid.uuid4()),
    )

    assert results == []
    qdrant_service.search_memories.assert_not_called()
    task_mock.delay.assert_not_called()
