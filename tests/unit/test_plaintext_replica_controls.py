from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock
from unittest.mock import MagicMock

import pytest

from api.services import retriever
from api.services.retriever import RetrieverService
from api.services.vector_outbox import build_vector_payload


class MemoryLike:
    id = "memory-1"
    content = "Private account preference"
    category = "fact"
    importance_score = 7.0
    confidence_score = 0.9
    is_archived = False
    agent_id = None
    previous_version_id = None
    source_event_id = None
    metadata_json = {}
    created_at = None
    last_accessed_at = None
    embedding_model_id = None
    embedding_model = None


def test_vector_payload_can_omit_plaintext_content(monkeypatch) -> None:
    monkeypatch.setattr(
        "api.services.vector_outbox.get_settings",
        lambda: SimpleNamespace(vector_payload_include_content=False),
    )

    payload = build_vector_payload(MemoryLike(), tenant_id="tenant-1", proxy_user_id="user-1")

    assert "content" not in payload
    assert payload["memory_id"] == "memory-1"
    assert payload["category"] == "fact"


@pytest.mark.asyncio
@pytest.mark.parametrize("include_content", [False, True])
async def test_vector_payload_always_requires_authorized_database_hydration(monkeypatch, include_content) -> None:
    from tests.unit.test_retriever import FakeEmbeddingService, FakeExecuteResult, FakeQuotaManager, make_memory
    memory = make_memory(content="Database-governed content")
    payload = {"memory_id": str(memory.id), "category": "fact", "importance_score": 7.0}
    if include_content:
        payload["content"] = "Forged vector payload content"
    session = MagicMock()
    session.execute = AsyncMock(side_effect=[
        FakeExecuteResult(scalar_value=10),
        FakeExecuteResult(items=[memory.embedding_model_id]),
        FakeExecuteResult(items=[memory]),
    ])
    cache = MagicMock()
    cache.get_retrieval_results = AsyncMock(return_value=None)
    cache.get_hot_memories = AsyncMock(return_value=None)
    qdrant = MagicMock()
    qdrant.breaker.current_state.return_value = "CLOSED"
    qdrant.search_memories_async = AsyncMock(return_value=[SimpleNamespace(id=str(memory.id), score=0.9, payload=payload)])
    service = RetrieverService(session=session, qdrant_service=qdrant, cache_service=cache,
        quota_manager=FakeQuotaManager(), embedding_service=FakeEmbeddingService())
    monkeypatch.setattr(service, "_queue_access_update", lambda _: None)
    results = await service.retrieve("query", user_id=str(memory.user_id), quota_mode="full")
    assert [row.content for row in results] == [memory.content]
    assert "memories.user_id" in str(session.execute.call_args.args[0])


@pytest.mark.asyncio
async def test_redis_cache_writes_can_be_disabled_without_changing_l1_cache(monkeypatch) -> None:
    monkeypatch.setattr(retriever, "REDIS_CACHE_WRITE_ENABLED", False)
    service = object.__new__(RetrieverService)
    service.cache_service = MagicMock()
    service.cache_service.set_retrieval_results = AsyncMock()
    service.cache_service.set_hot_memories = AsyncMock()

    await service._write_retrieval_cache("user-1", "default", [{"content": "private"}])

    service.cache_service.set_retrieval_results.assert_not_awaited()
    service.cache_service.set_hot_memories.assert_not_awaited()
