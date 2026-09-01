from __future__ import annotations

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from pydantic import ValidationError
from sqlalchemy.dialects import postgresql

from api.db.models import Memory, MemoryCategory
from api.schemas.requests import MemoryRetrieveRequest
from api.services.retriever import RetrieverService


class _ScalarRows:
    def __init__(self, rows: list[Memory]) -> None:
        self._rows = rows

    def scalars(self) -> _ScalarRows:
        return self

    def all(self) -> list[Memory]:
        return self._rows


class _Session:
    def __init__(self, rows: list[Memory]) -> None:
        self.rows = rows
        self.statement = None

    async def execute(self, statement):
        self.statement = statement
        return _ScalarRows(self.rows)


def _memory(*, archived: bool) -> Memory:
    return Memory(
        id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        proxy_user_id=uuid.uuid4(),
        content="User lived in Delhi.",
        category=MemoryCategory.fact,
        importance_score=7.0,
        confidence_score=0.9,
        embedding_id=str(uuid.uuid4()),
        embedding_model_id="gemini-embedding-001",
        source_conversation_id=uuid.uuid4(),
        effective_from=datetime(2026, 1, 1, tzinfo=UTC),
        effective_until=datetime(2026, 4, 1, tzinfo=UTC),
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
        last_accessed_at=datetime(2026, 1, 1, tzinfo=UTC),
        metadata_json={},
        is_archived=archived,
    )


def test_as_of_is_optional_and_requires_timezone() -> None:
    current = MemoryRetrieveRequest(external_user_id="u1", query="location")
    historical = MemoryRetrieveRequest(
        external_user_id="u1",
        query="location",
        as_of="2026-02-01T00:00:00Z",
    )

    assert current.as_of is None
    assert historical.as_of == datetime(2026, 2, 1, tzinfo=UTC)
    with pytest.raises(ValidationError, match="timezone"):
        MemoryRetrieveRequest(
            external_user_id="u1",
            query="location",
            as_of="2026-02-01T00:00:00",
        )


def test_as_of_cache_context_is_isolated_from_current_and_other_dates() -> None:
    current = RetrieverService._cache_context("location", [], None, 10)
    february = RetrieverService._cache_context(
        "location", [], None, 10, as_of=datetime(2026, 2, 1, tzinfo=UTC)
    )
    march = RetrieverService._cache_context(
        "location", [], None, 10, as_of=datetime(2026, 3, 1, tzinfo=UTC)
    )

    assert len({current, february, march}) == 3


@pytest.mark.asyncio
async def test_as_of_postgres_query_uses_validity_and_can_return_archived_revision() -> None:
    historical_memory = _memory(archived=True)
    session = _Session([historical_memory])
    service = object.__new__(RetrieverService)
    service.session = session

    results = await service._retrieve_as_of_memories(
        proxy_user_id=str(historical_memory.proxy_user_id),
        user_id=None,
        as_of=datetime(2026, 2, 1, tzinfo=UTC),
        limit=10,
        categories=["fact"],
        agent_id=None,
        created_after=None,
    )

    sql = str(
        session.statement.compile(
            dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}
        )
    )
    assert results[0].id == str(historical_memory.id)
    assert "effective_from" in sql
    assert "effective_until" in sql
    assert "memories.is_archived IS false" in sql


@pytest.mark.asyncio
async def test_as_of_service_rejects_naive_datetime_for_direct_callers() -> None:
    service = object.__new__(RetrieverService)
    service.session = _Session([])
    with pytest.raises(ValueError, match="timezone"):
        await service._retrieve_as_of_memories(
            proxy_user_id=str(uuid.uuid4()),
            user_id=None,
            as_of=datetime(2026, 2, 1),
            limit=10,
            categories=[],
            agent_id=None,
            created_after=None,
        )
