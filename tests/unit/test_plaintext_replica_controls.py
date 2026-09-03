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


def test_contentless_qdrant_payload_requests_authorized_database_fallback() -> None:
    service = object.__new__(RetrieverService)
    point = SimpleNamespace(
        id="memory-1",
        score=0.9,
        payload={"memory_id": "memory-1", "category": "fact", "importance_score": 7.0},
    )

    assert service._results_from_qdrant_payloads([point]) == []


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
