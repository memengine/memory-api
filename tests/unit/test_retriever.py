from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from api.db.models import Memory, MemoryCategory, QuotaMode
from api.errors import APIError
from api.services.embedding_service import DEFAULT_ACTIVE_MODEL_ID, EmbeddingResult
from api.services.retriever import MemoryResult, RetrieverService


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


class FailingEmbeddingService(FakeEmbeddingService):
    async def embed(self, text: str, model_id: str | None = None) -> EmbeddingResult:
        raise TimeoutError("embedding connection timed out")


def test_semantic_floor_preserves_boundary_and_rejects_lower_score() -> None:
    service = object.__new__(RetrieverService)

    assert service._passes_semantic_floor(SimpleNamespace(score=0.315)) is True
    assert service._passes_semantic_floor(SimpleNamespace(score=0.314999)) is False

def test_qdrant_payload_results_include_provenance() -> None:
    memory_id = uuid.uuid4()
    source_event_id = uuid.uuid4()
    service = object.__new__(RetrieverService)
    point = SimpleNamespace(
        id=str(memory_id),
        score=0.91,
        payload={
            "memory_id": str(memory_id),
            "content": "User's current subscription plan is Growth.",
            "category": "fact",
            "importance_score": 7.0,
            "confidence_score": 0.95,
            "agent_id": None,
            "previous_version_id": None,
            "created_at": datetime.now(UTC).isoformat(),
            "last_accessed_at": None,
            "source_event_id": str(source_event_id),
            "provenance": {
                "service": "billing-service",
                "event_id": "billing-plan-001",
                "writer_id": str(uuid.uuid4()),
            },
        },
    )

    results = service._results_from_qdrant_payloads([point])

    assert len(results) == 1
    assert results[0].source_event_id == str(source_event_id)
    assert results[0].provenance == point.payload["provenance"]


def test_qdrant_payload_results_enforce_requested_agent() -> None:
    requested_agent_id = str(uuid.uuid4())
    other_agent_id = str(uuid.uuid4())
    service = object.__new__(RetrieverService)

    def point(agent_id: str | None) -> SimpleNamespace:
        memory_id = uuid.uuid4()
        return SimpleNamespace(
            id=str(memory_id),
            score=0.9,
            payload={
                "memory_id": str(memory_id),
                "content": "Agent-scoped memory",
                "category": "fact",
                "importance_score": 5.0,
                "confidence_score": 0.9,
                "agent_id": agent_id,
                "created_at": datetime.now(UTC).isoformat(),
            },
        )

    results = service._results_from_qdrant_payloads(
        [point(requested_agent_id), point(other_agent_id), point(None)],
        agent_id=requested_agent_id,
    )

    assert len(results) == 1
    assert results[0].agent_id == requested_agent_id


@pytest.mark.asyncio
async def test_retrieve_returns_cached_results_immediately(monkeypatch) -> None:
    monkeypatch.setattr("api.services.retriever.REDIS_CACHE_READ_ENABLED", True)
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
async def test_retrieve_reports_embedding_dependency_failure_instead_of_empty_result(
    monkeypatch,
) -> None:
    user_id = str(uuid.uuid4())
    session = MagicMock()
    session.execute = AsyncMock(
        side_effect=[
            FakeExecuteResult(scalar_value=10),
            FakeExecuteResult(scalar_value=make_embedding_model()),
        ]
    )
    cache_service = MagicMock()
    cache_service.get_hot_memories = AsyncMock(return_value=None)
    qdrant_service = MagicMock()
    qdrant_service.breaker.current_state.return_value = "CLOSED"
    monkeypatch.setattr(
        "api.services.retriever.update_memory_accesses", MagicMock()
    )
    service = RetrieverService(
        session=session,
        qdrant_service=qdrant_service,
        cache_service=cache_service,
        quota_manager=FakeQuotaManager(),
        embedding_service=FailingEmbeddingService(),
    )

    with pytest.raises(APIError) as captured:
        await service.retrieve(query="launch", user_id=user_id, limit=10)

    assert captured.value.status_code == 503
    assert captured.value.code == "EMB_503"
    qdrant_service.search_memories.assert_not_called()


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


def test_local_cache_invalidation_is_scoped_to_one_proxy_user() -> None:
    payload = {
        "id": "memory-1", "content": "Scoped", "category": "fact",
        "importance_score": 7.0, "confidence_score": .9,
        "semantic_score": .9, "recency_score": 1.0, "final_score": .9,
        "agent_id": None, "previous_version_id": None, "last_accessed_at": None,
    }
    result = RetrieverService._memory_result_from_cache(payload)
    RetrieverService._l1_cache = {
        "proxy-1|query": (float("inf"), [result]),
        "proxy-2|query": (float("inf"), [result]),
    }
    RetrieverService._hot_tier_cache = {
        "proxy-1|||": (float("inf"), [result]),
        "proxy-2|||": (float("inf"), [result]),
    }
    RetrieverService._memory_count_cache = {
        "proxy:proxy-1": (float("inf"), 1),
        "proxy:proxy-2": (float("inf"), 1),
    }

    RetrieverService.invalidate_local_user_cache("proxy-1")

    assert set(RetrieverService._l1_cache) == {"proxy-2|query"}
    assert set(RetrieverService._hot_tier_cache) == {"proxy-2|||"}
    assert set(RetrieverService._memory_count_cache) == {"proxy:proxy-2"}


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
async def test_retrieve_reports_dependency_error_when_embedding_fails(monkeypatch) -> None:
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

    service = RetrieverService(
        session=session,
        qdrant_service=qdrant_service,
        cache_service=cache_service,
        quota_manager=FakeQuotaManager(),
        embedding_service=FailingEmbeddingService(),
    )

    with pytest.raises(APIError) as captured:
        await service.retrieve(
            query="python",
            user_id=str(uuid.uuid4()),
        )

    assert captured.value.status_code == 503
    assert captured.value.code == "EMB_503"
    qdrant_service.search_memories.assert_not_called()
    task_mock.delay.assert_not_called()
