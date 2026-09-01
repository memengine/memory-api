from __future__ import annotations

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from api.routers.universal import _search_universal_memories


@pytest.mark.asyncio
async def test_universal_retrieval_returns_stored_source_agent_provenance(monkeypatch) -> None:
    memory_id = uuid.uuid4()
    source_agent_id = uuid.uuid4()
    user_id = uuid.uuid4()
    memory = SimpleNamespace(
        id=memory_id, user_uui_id=user_id, source_agent_id=source_agent_id,
        content="User works remotely.", category="fact", importance_score=7.0,
        confidence=0.95, last_accessed_at=datetime.now(UTC), created_at=datetime.now(UTC),
        metadata_json={"provenance": {"source_agent_id": str(source_agent_id), "event_id": "evt-1"}},
    )

    class Result:
        def scalars(self): return SimpleNamespace(all=lambda: [memory])

    session = SimpleNamespace(
        execute=lambda *_args, **_kwargs: Result(),
        commit=lambda: None,
    )
    async def execute(*args, **kwargs): return Result()
    async def commit(): return None
    session.execute = execute
    session.commit = commit

    monkeypatch.setattr(
        "api.routers.universal.EmbeddingService",
        lambda **_kwargs: SimpleNamespace(embed=lambda *_args, **_kwargs: None),
    )
    async def embed(*_args, **_kwargs):
        return SimpleNamespace(vector=[0.1], dimensions=1)
    monkeypatch.setattr("api.routers.universal.EmbeddingService", lambda **_kwargs: SimpleNamespace(embed=embed))
    point = SimpleNamespace(id=str(memory_id), score=0.9)
    qdrant = SimpleNamespace(
        _ensure_collection_if_possible=lambda **_kwargs: None,
        client=SimpleNamespace(query_points=lambda **_kwargs: SimpleNamespace(points=[point])),
    )
    context = SimpleNamespace(
        build_context=lambda *_args, **_kwargs: "- memory",
        build=lambda *_args, **_kwargs: SimpleNamespace(system_prompt_addition="", token_count=1),
    )

    data, _, _ = await _search_universal_memories(
        session=session, qdrant_service=qdrant, context_builder=context,
        query="work arrangement", user=SimpleNamespace(id=user_id),
        allowed_categories=["fact"], limit=5, format="bullets", context_max_tokens=100,
    )

    assert data[0].provenance == {
        "source_agent_id": str(source_agent_id), "event_id": "evt-1"
    }
