from __future__ import annotations

import uuid
from unittest.mock import AsyncMock
from unittest.mock import MagicMock

import pytest

from api.db.models import MemoryCategory
from api.schemas.requests import MemoryRetrieveRequest
from api.services.retriever_service import RetrieverService
from tests.unit.test_retriever import FakeEmbeddingService
from tests.unit.test_retriever import FakeExecuteResult
from tests.unit.test_retriever import FakeQuotaManager
from tests.unit.test_retriever import make_memory


def test_retriever_service_alias_exports_existing_implementation() -> None:
    assert RetrieverService.__name__ == "RetrieverService"
    assert round(
        RetrieverService.SEMANTIC_WEIGHT
        + RetrieverService.IMPORTANCE_WEIGHT
        + RetrieverService.RECENCY_WEIGHT,
        3,
    ) == 1.0


def test_retrieve_request_accepts_time_filter_days() -> None:
    request = MemoryRetrieveRequest(
        external_user_id="user-123",
        query="python preferences",
        time_filter_days=30,
        categories=["preference"],
    )

    assert request.time_filter_days == 30
    assert request.categories == ["preference"]


def test_cache_context_includes_time_filter_when_present() -> None:
    base_context = RetrieverService._cache_context("Python", ["preference"], None, 10)
    timed_context = RetrieverService._cache_context("Python", ["preference"], None, 10, 30)

    assert base_context == "python|preference||10"
    assert timed_context == "python|preference||10|30"
    assert base_context != timed_context


@pytest.mark.asyncio
async def test_qdrant_open_uses_postgres_fallback(monkeypatch) -> None:
    user_id = str(uuid.uuid4())
    memory = make_memory(
        content="User prefers Python examples",
        category=MemoryCategory.preference,
        importance_score=8.0,
    )

    session = MagicMock()
    session.execute = AsyncMock(
        side_effect=[
            FakeExecuteResult(scalar_value=10),
            FakeExecuteResult(items=[memory]),
        ]
    )
    cache_service = MagicMock()
    cache_service.get_hot_memories = AsyncMock(return_value=None)
    cache_service.set_hot_memories = AsyncMock()
    cache_service.get_hot_tier_memories = AsyncMock(return_value=[])
    qdrant_service = MagicMock()
    qdrant_service.breaker.current_state.return_value = "OPEN"
    task_mock = MagicMock()
    monkeypatch.setattr("api.services.retriever.update_memory_accesses", task_mock)

    service = RetrieverService(
        session=session,
        qdrant_service=qdrant_service,
        cache_service=cache_service,
        quota_manager=FakeQuotaManager(),
        embedding_service=FakeEmbeddingService(),
    )

    results = await service.retrieve(
        query="python examples",
        user_id=user_id,
        tenant_id=str(uuid.uuid4()),
    )

    assert len(results) == 1
    assert results[0].content == "User prefers Python examples"
    assert service.last_is_degraded is True
    qdrant_service.search_memories.assert_not_called()
    task_mock.delay.assert_called_once()
