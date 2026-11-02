"""Independent scope associations must survive content deduplication."""
import json
from dataclasses import asdict
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from api.services.retriever import MemoryResult, RetrieverService
from tests.unit.test_retriever import make_memory, FakeEmbeddingService


def result(scope, *, score=1.0, text="This project uses Python."):
    return MemoryResult(id=str(id(scope)), content=text, category="expertise", importance_score=5,
        confidence_score=1, semantic_score=1, recency_score=1, final_score=score,
        agent_id=None, previous_version_id=None, last_accessed_at=None,
        provenance={"scope": scope})


@pytest.mark.parametrize("scopes", [
    [{"project": 1}, {"project": "1"}, {"project": True}, {"project": "True"}],
    [{"project": "alpha"}, {"project": "beta"}, {}, {"project": None}],
    [{"nested": {"id": 1}}, {"nested": {"id": "1"}}],
])
def test_independent_scopes_survive_similarity_dedup(scopes):
    rows = [result(scope, score=1-index/100) for index, scope in enumerate(scopes)]
    assert RetrieverService._deduplicate_results(rows) == rows
    cached = [RetrieverService._memory_result_from_cache(json.loads(json.dumps(asdict(row)))) for row in rows]
    assert len(RetrieverService._deduplicate_results(cached)) == len(rows)


def test_same_scope_equivalent_key_order_still_deduplicates():
    higher = result({"project": "alpha", "region": "in"}, score=0.9)
    lower = result({"region": "in", "project": "alpha"}, score=0.8, text="This project uses Python")
    assert RetrieverService._deduplicate_results([lower, higher]) == [higher]


@pytest.mark.asyncio
async def test_vector_ranking_keeps_scopes_before_limit(monkeypatch):
    import uuid
    from unittest.mock import MagicMock
    from tests.unit.test_retriever import FakeExecuteResult, FakeQuotaManager, make_embedding_model, make_point
    user_id = str(uuid.uuid4())
    memories = []
    for project in (1, "1", True, "True"):
        memory = make_memory(content="This project uses Python.")
        memory.metadata_json = {"provenance": {"scope": {"project": project}}}
        memories.append(memory)
    session = MagicMock()
    session.execute = AsyncMock(side_effect=[FakeExecuteResult(scalar_value=8),
        FakeExecuteResult(scalar_value=make_embedding_model()), FakeExecuteResult(items=memories)])
    cache = MagicMock()
    cache.get_hot_memories = AsyncMock(return_value=None)
    cache.set_hot_memories = AsyncMock()
    vector = MagicMock()
    vector.search_memories.return_value = [make_point(m, 0.9-index/100) for index,m in enumerate(memories)]
    monkeypatch.setattr(RetrieverService, "_finish_read_transaction", AsyncMock())
    monkeypatch.setattr("api.services.retriever.update_memory_accesses", MagicMock())
    service = RetrieverService(session=session, qdrant_service=vector, cache_service=cache,
        quota_manager=FakeQuotaManager(), embedding_service=FakeEmbeddingService())
    found = await service.retrieve(query="project languages", user_id=user_id, limit=30)
    assert {r.id for r in found} == {str(m.id) for m in memories}


@pytest.mark.asyncio
async def test_sql_cold_start_keeps_all_typed_scope_records():
    rows = []
    for project in (1, "1", True, "True"):
        memory = make_memory(content="This project uses Python.")
        memory.metadata_json = {"provenance": {"scope": {"project": project}}}
        rows.append(memory)
    service = RetrieverService(session=SimpleNamespace(), qdrant_service=None,
        cache_service=None, quota_manager=None, embedding_service=FakeEmbeddingService())
    service._fetch_cold_start_memories = AsyncMock(return_value=rows)
    results = await service._retrieve_cold_start_memories(proxy_user_id=str(rows[0].proxy_user_id),
        limit=30, categories=[], agent_id=None, created_after=None)
    assert {r.id for r in results} == {str(m.id) for m in rows}
