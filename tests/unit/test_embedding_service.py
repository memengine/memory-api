from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock
from unittest.mock import MagicMock

import pytest

from api.services.embedding_service import DEFAULT_ACTIVE_MODEL_ID
from api.services.embedding_service import EmbeddingService


@pytest.fixture(autouse=True)
def clear_embedding_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("EMBEDDING_PROVIDER", raising=False)
    monkeypatch.delenv("EMBEDDING_MODEL", raising=False)
    monkeypatch.delenv("EMBEDDING_MODEL_ID", raising=False)
    monkeypatch.delenv("EMBEDDING_DIMENSIONS", raising=False)
    monkeypatch.delenv("QDRANT_COLLECTION", raising=False)


class FakeAsyncRedis:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}

    async def get(self, key: str):
        return self.values.get(key)

    async def set(self, key: str, value: str, ex: int | None = None):
        self.values[key] = value
        return True

    async def delete(self, key: str):
        self.values.pop(key, None)
        return 1


class FakeSyncRedis:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}

    def get(self, key: str):
        return self.values.get(key)

    def set(self, key: str, value: str, ex: int | None = None):
        self.values[key] = value
        return True

    def delete(self, key: str):
        self.values.pop(key, None)
        return 1


class FakeAsyncExecuteResult:
    def __init__(self, scalar=None, scalars=None) -> None:
        self._scalar = scalar
        self._scalars = scalars or []

    def scalar_one_or_none(self):
        return self._scalar

    def scalars(self):
        return self

    def all(self):
        return list(self._scalars)


class FakeSyncExecuteResult(FakeAsyncExecuteResult):
    pass


def make_model(
    *,
    model_id: str = DEFAULT_ACTIVE_MODEL_ID,
    provider: str = "gemini",
    model_name: str = "gemini-embedding-001",
    dimensions: int = 1536,
    qdrant_collection: str = "memories",
    is_active: bool = True,
):
    return SimpleNamespace(
        id=model_id,
        provider=SimpleNamespace(value=provider),
        model_name=model_name,
        dimensions=dimensions,
        qdrant_collection=qdrant_collection,
        is_active=is_active,
        deprecated_at=None,
        created_at=SimpleNamespace(isoformat=lambda: "2026-04-01T00:00:00+00:00"),
    )


@pytest.mark.asyncio
async def test_get_active_model_uses_cache_after_first_lookup() -> None:
    model = make_model()
    session = MagicMock()
    session.execute = AsyncMock(return_value=FakeAsyncExecuteResult(scalar=model))
    service = EmbeddingService(
        async_session=session,
        async_redis_client=FakeAsyncRedis(),
        sync_redis_client=FakeSyncRedis(),
        gemini_client=MagicMock(),
    )

    first = await service.get_active_model()
    second = await service.get_active_model()

    assert first.id == DEFAULT_ACTIVE_MODEL_ID
    assert second.id == DEFAULT_ACTIVE_MODEL_ID
    session.execute.assert_awaited_once()


def test_get_active_model_sync_prefers_database_record_over_environment_defaults(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_model = make_model(
        model_id="openai-text-embedding-3-small-v1",
        provider="openai",
        model_name="text-embedding-3-small",
        qdrant_collection="memories_openai",
    )
    session = MagicMock()
    session.execute.return_value = FakeSyncExecuteResult(scalar=database_model)
    monkeypatch.setenv("EMBEDDING_MODEL_ID", "incorrect-environment-model")
    monkeypatch.setenv("QDRANT_COLLECTION", "incorrect-environment-collection")
    service = EmbeddingService(
        sync_session=session,
        async_redis_client=FakeAsyncRedis(),
        sync_redis_client=FakeSyncRedis(),
        gemini_client=MagicMock(),
    )

    model = service.get_active_model_sync()

    assert model.id == "openai-text-embedding-3-small-v1"
    assert model.qdrant_collection == "memories_openai"


@pytest.mark.asyncio
async def test_embed_uses_requested_model_and_returns_metadata() -> None:
    model = make_model(model_id="gemini-embedding-001-v2", qdrant_collection="memories_v2")
    session = MagicMock()
    session.execute = AsyncMock(return_value=FakeAsyncExecuteResult(scalar=model))
    gemini_client = MagicMock()
    gemini_client.models.embed_content = MagicMock(
        return_value=SimpleNamespace(embeddings=[SimpleNamespace(values=[0.1, 0.2, 0.3])])
    )

    service = EmbeddingService(
        async_session=session,
        async_redis_client=FakeAsyncRedis(),
        sync_redis_client=FakeSyncRedis(),
        gemini_client=gemini_client,
    )

    result = await service.embed("hello world", model_id="gemini-embedding-001-v2")

    assert result.model_id == "gemini-embedding-001-v2"
    assert result.dimensions == 1536
    assert result.qdrant_collection == "memories_v2"
    assert result.vector == [0.1, 0.2, 0.3]


def test_list_models_sync_returns_all_models() -> None:
    models = [
        make_model(model_id="gemini-embedding-001-v1"),
        make_model(model_id="gemini-embedding-002-v2", qdrant_collection="memories_v2", is_active=False),
    ]
    session = MagicMock()
    session.execute.return_value = FakeSyncExecuteResult(scalars=models)
    service = EmbeddingService(
        sync_session=session,
        async_redis_client=FakeAsyncRedis(),
        sync_redis_client=FakeSyncRedis(),
        gemini_client=MagicMock(),
    )

    records = service.list_models_sync()

    assert [record.id for record in records] == [
        "gemini-embedding-001-v1",
        "gemini-embedding-002-v2",
    ]


def test_set_active_model_sync_invalidates_and_rebuilds_cache() -> None:
    new_model = make_model(
        model_id="gemini-embedding-001-v2",
        qdrant_collection="memories_v2",
        is_active=False,
    )
    session = MagicMock()
    session.execute.return_value = FakeSyncExecuteResult(scalar=new_model)
    redis_client = FakeSyncRedis()
    redis_client.values["embedding_models:active"] = '{"id":"stale","provider":"gemini","model_name":"old","dimensions":1536,"qdrant_collection":"memories","is_active":true,"deprecated_at":null,"created_at":"2026-04-01T00:00:00+00:00"}'
    service = EmbeddingService(
        sync_session=session,
        async_redis_client=FakeAsyncRedis(),
        sync_redis_client=redis_client,
        gemini_client=MagicMock(),
    )

    result = service.set_active_model_sync("gemini-embedding-001-v2")

    assert result.id == "gemini-embedding-001-v2"
    assert "stale" not in redis_client.values["embedding_models:active"]
    assert "gemini-embedding-001-v2" in redis_client.values["embedding_models:active"]
    assert session.commit.call_count == 1


@pytest.mark.asyncio
async def test_local_provider_uses_http_endpoint() -> None:
    model = make_model(
        model_id="local-embed-v1",
        provider="local",
        model_name="bge-small",
        dimensions=384,
        qdrant_collection="memories_local",
    )
    session = MagicMock()
    session.execute = AsyncMock(return_value=FakeAsyncExecuteResult(scalar=model))
    http_client = MagicMock()
    response = MagicMock()
    response.json.return_value = {"embedding": [0.4, 0.5]}
    response.raise_for_status.return_value = None
    http_client.post = MagicMock(return_value=response)

    service = EmbeddingService(
        async_session=session,
        async_redis_client=FakeAsyncRedis(),
        sync_redis_client=FakeSyncRedis(),
        gemini_client=MagicMock(),
        sync_http_client=http_client,
    )

    result = await service.embed("local text", model_id="local-embed-v1")

    assert result.model_id == "local-embed-v1"
    assert result.vector == [0.4, 0.5]
    http_client.post.assert_called_once()
