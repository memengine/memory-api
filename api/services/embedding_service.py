from __future__ import annotations

import asyncio
import json
import os
from dataclasses import asdict
from dataclasses import dataclass
from datetime import UTC
from datetime import datetime
from typing import Any

import httpx
import redis
import redis.asyncio as redis_async
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from api.db.models import EmbeddingModel
from api.db.models import EmbeddingProvider
from api.infra.llm_providers import CohereProvider
from api.infra.llm_providers import GeminiProvider
from api.infra.llm_providers import LocalProvider
from api.infra.llm_providers import OpenAIProvider
from api.infra.llm_router import EmbeddingUnavailableError
from api.infra.llm_router import LLMRouter
from api.settings import get_settings


DEFAULT_ACTIVE_MODEL_ID = "openai-text-embedding-3-small-v1"
ACTIVE_MODEL_CACHE_KEY = "embedding_models:active"
ACTIVE_MODEL_CACHE_TTL_SECONDS = 300
REDIS_CONNECT_TIMEOUT_SECONDS = 0.2
REDIS_IO_TIMEOUT_SECONDS = 0.2


def _require_redis_url() -> str:
    redis_url = os.getenv("REDIS_URL") or get_settings().redis_url
    if not redis_url:
        raise RuntimeError("REDIS_URL is required.")
    return redis_url


@dataclass(slots=True)
class EmbeddingModelRecord:
    id: str
    provider: str
    model_name: str
    dimensions: int
    qdrant_collection: str
    is_active: bool
    deprecated_at: str | None
    created_at: str


@dataclass(slots=True)
class EmbeddingResult:
    vector: list[float]
    model_id: str
    dimensions: int
    qdrant_collection: str


class EmbeddingService:
    def __init__(
        self,
        *,
        async_session: AsyncSession | None = None,
        sync_session: Session | None = None,
        async_redis_client: Any | None = None,
        sync_redis_client: Any | None = None,
        gemini_client: Any | None = None,
        async_http_client: httpx.AsyncClient | None = None,
        sync_http_client: httpx.Client | None = None,
        local_endpoint: str | None = None,
        llm_router: LLMRouter | None = None,
    ) -> None:
        self.async_session = async_session
        self.sync_session = sync_session
        self.async_redis_client = async_redis_client or redis_async.from_url(
            _require_redis_url(),
            encoding="utf-8",
            decode_responses=True,
            socket_connect_timeout=REDIS_CONNECT_TIMEOUT_SECONDS,
            socket_timeout=REDIS_IO_TIMEOUT_SECONDS,
        )
        self.sync_redis_client = sync_redis_client or redis.from_url(
            _require_redis_url(),
            encoding="utf-8",
            decode_responses=True,
            socket_connect_timeout=REDIS_CONNECT_TIMEOUT_SECONDS,
            socket_timeout=REDIS_IO_TIMEOUT_SECONDS,
        )
        self.gemini_client = gemini_client
        self.async_http_client = async_http_client or httpx.AsyncClient(timeout=5.0)
        self.sync_http_client = sync_http_client or httpx.Client(timeout=5.0)
        self.local_endpoint = local_endpoint or os.getenv("LOCAL_EMBEDDING_ENDPOINT") or get_settings().local_embedding_endpoint
        self.llm_router = llm_router or LLMRouter(
            sync_session=sync_session,
            redis_client=self.sync_redis_client,
            provider_factories={
                "gemini": lambda **overrides: GeminiProvider(
                    client=overrides.pop("client", None) or self.gemini_client,
                    api_key=overrides.pop("api_key", None),
                    **overrides,
                ),
                "cohere": lambda **overrides: CohereProvider(
                    http_client=overrides.pop("http_client", None) or self.sync_http_client,
                    api_key=overrides.pop("api_key", None),
                    **overrides,
                ),
                "local": lambda **overrides: LocalProvider(
                    http_client=overrides.pop("http_client", None) or self.sync_http_client,
                    endpoint=overrides.pop("endpoint", None) or self.local_endpoint,
                    **overrides,
                ),
                "openai": lambda **overrides: OpenAIProvider(
                    http_client=overrides.pop("http_client", None) or self.sync_http_client,
                    api_key=overrides.pop("api_key", None),
                    **overrides,
                ),
            },
        )

    async def embed(
        self,
        text: str,
        model_id: str | None = None,
        tenant_id: str | None = None,
    ) -> EmbeddingResult:
        model = await (self.get_model(model_id) if model_id else self.get_active_model())
        vector = await asyncio.to_thread(self._embed_with_model_sync, text, model, tenant_id)
        return EmbeddingResult(
            vector=vector,
            model_id=model.id,
            dimensions=model.dimensions,
            qdrant_collection=model.qdrant_collection,
        )

    def embed_sync(
        self,
        text: str,
        model_id: str | None = None,
        tenant_id: str | None = None,
    ) -> EmbeddingResult:
        model = self.get_model_sync(model_id) if model_id else self.get_active_model_sync()
        vector = self._embed_with_model_sync(text, model, tenant_id)
        return EmbeddingResult(
            vector=vector,
            model_id=model.id,
            dimensions=model.dimensions,
            qdrant_collection=model.qdrant_collection,
        )

    async def get_active_model(self) -> EmbeddingModelRecord:
        if self._env_model_overrides_active_model():
            return self._default_model_record()

        cached = await self._get_cached_active_model_async()
        if cached is not None:
            return cached

        if self.async_session is None:
            return self._default_model_record()

        result = await self.async_session.execute(
            select(EmbeddingModel).where(EmbeddingModel.is_active.is_(True)).limit(1)
        )
        model = result.scalar_one_or_none()
        if model is None or not self._is_embedding_model_like(model):
            return self._default_model_record()

        record = self._to_record(model)
        await self._cache_active_model_async(record)
        return record

    def get_active_model_sync(self) -> EmbeddingModelRecord:
        if self._env_model_overrides_active_model():
            return self._default_model_record()

        cached = self._get_cached_active_model_sync()
        if cached is not None:
            return cached

        if self.sync_session is None:
            return self._default_model_record()

        model = self.sync_session.execute(
            select(EmbeddingModel).where(EmbeddingModel.is_active.is_(True)).limit(1)
        ).scalar_one_or_none()
        if model is None or not self._is_embedding_model_like(model):
            return self._default_model_record()

        record = self._to_record(model)
        self._cache_active_model_sync(record)
        return record

    async def get_model(self, model_id: str) -> EmbeddingModelRecord:
        if self.async_session is None:
            default = self._default_model_record()
            if model_id != default.id:
                raise ValueError(f"Embedding model '{model_id}' not found.")
            return default

        result = await self.async_session.execute(
            select(EmbeddingModel).where(EmbeddingModel.id == model_id).limit(1)
        )
        model = result.scalar_one_or_none()
        if model is None or not self._is_embedding_model_like(model):
            raise ValueError(f"Embedding model '{model_id}' not found.")
        return self._to_record(model)

    def get_model_sync(self, model_id: str) -> EmbeddingModelRecord:
        if self.sync_session is None:
            default = self._default_model_record()
            if model_id != default.id:
                raise ValueError(f"Embedding model '{model_id}' not found.")
            return default

        model = self.sync_session.execute(
            select(EmbeddingModel).where(EmbeddingModel.id == model_id).limit(1)
        ).scalar_one_or_none()
        if model is None or not self._is_embedding_model_like(model):
            raise ValueError(f"Embedding model '{model_id}' not found.")
        return self._to_record(model)

    async def list_models(self) -> list[EmbeddingModelRecord]:
        if self.async_session is None:
            return [self._default_model_record()]

        result = await self.async_session.execute(
            select(EmbeddingModel).order_by(EmbeddingModel.created_at, EmbeddingModel.id)
        )
        return [self._to_record(model) for model in result.scalars().all()]

    def list_models_sync(self) -> list[EmbeddingModelRecord]:
        if self.sync_session is None:
            return [self._default_model_record()]

        result = self.sync_session.execute(
            select(EmbeddingModel).order_by(EmbeddingModel.created_at, EmbeddingModel.id)
        )
        return [self._to_record(model) for model in result.scalars().all()]

    async def set_active_model(self, model_id: str) -> EmbeddingModelRecord:
        if self.async_session is None:
            raise RuntimeError("EmbeddingService requires async_session to change active model.")

        target = await self.get_model(model_id)
        await self.async_session.execute(
            EmbeddingModel.__table__.update()
            .where(EmbeddingModel.is_active.is_(True), EmbeddingModel.id != model_id)
            .values(is_active=False, deprecated_at=datetime.now(UTC))
        )
        await self.async_session.execute(
            EmbeddingModel.__table__.update()
            .where(EmbeddingModel.id == model_id)
            .values(is_active=True, deprecated_at=None)
        )
        await self.async_session.commit()
        await self._invalidate_active_model_cache_async()
        await self._cache_active_model_async(
            target.__class__(
                **{**asdict(target), "is_active": True}
            )
        )
        return target.__class__(**{**asdict(target), "is_active": True})

    def set_active_model_sync(self, model_id: str) -> EmbeddingModelRecord:
        if self.sync_session is None:
            raise RuntimeError("EmbeddingService requires sync_session to change active model.")

        target = self.get_model_sync(model_id)
        self.sync_session.execute(
            EmbeddingModel.__table__.update()
            .where(EmbeddingModel.is_active.is_(True), EmbeddingModel.id != model_id)
            .values(is_active=False, deprecated_at=datetime.now(UTC))
        )
        self.sync_session.execute(
            EmbeddingModel.__table__.update()
            .where(EmbeddingModel.id == model_id)
            .values(is_active=True, deprecated_at=None)
        )
        self.sync_session.commit()
        self._invalidate_active_model_cache_sync()
        self._cache_active_model_sync(
            target.__class__(**{**asdict(target), "is_active": True})
        )
        return target.__class__(**{**asdict(target), "is_active": True})

    def _embed_with_model_sync(
        self,
        text: str,
        model: EmbeddingModelRecord,
        tenant_id: str | None,
    ) -> list[float]:
        provider_name = EmbeddingProvider(model.provider).value
        provider_kwargs: dict[str, object]
        if provider_name in {"gemini", "cohere", "openai"}:
            provider_kwargs = {
                "embed_model": model.model_name,
                "embedding_dimensions": model.dimensions,
            }
        elif provider_name == "local":
            provider_kwargs = {
                "model_name": model.model_name,
                "embedding_dimensions": model.dimensions,
            }
        else:
            provider_kwargs = {"embedding_dimensions": model.dimensions}

        provider = self.llm_router.get_provider(provider_name, **provider_kwargs)
        if not self.llm_router._provider_available(provider, capability="embed"):
            raise EmbeddingUnavailableError(f"{provider.provider_name} embedding unavailable.")
        return provider.embed(text)

    async def _get_cached_active_model_async(self) -> EmbeddingModelRecord | None:
        try:
            raw = await self.async_redis_client.get(ACTIVE_MODEL_CACHE_KEY)
        except Exception:
            return None
        return self._record_from_cache(raw)

    def _get_cached_active_model_sync(self) -> EmbeddingModelRecord | None:
        try:
            raw = self.sync_redis_client.get(ACTIVE_MODEL_CACHE_KEY)
        except Exception:
            return None
        return self._record_from_cache(raw)

    async def _cache_active_model_async(self, model: EmbeddingModelRecord) -> None:
        try:
            await self.async_redis_client.set(
                ACTIVE_MODEL_CACHE_KEY,
                json.dumps(asdict(model)),
                ex=ACTIVE_MODEL_CACHE_TTL_SECONDS,
            )
        except Exception:
            return None

    def _cache_active_model_sync(self, model: EmbeddingModelRecord) -> None:
        try:
            self.sync_redis_client.set(
                ACTIVE_MODEL_CACHE_KEY,
                json.dumps(asdict(model)),
                ex=ACTIVE_MODEL_CACHE_TTL_SECONDS,
            )
        except Exception:
            return None

    async def _invalidate_active_model_cache_async(self) -> None:
        try:
            await self.async_redis_client.delete(ACTIVE_MODEL_CACHE_KEY)
        except Exception:
            return None

    def _invalidate_active_model_cache_sync(self) -> None:
        try:
            self.sync_redis_client.delete(ACTIVE_MODEL_CACHE_KEY)
        except Exception:
            return None

    @staticmethod
    def _record_from_cache(raw: Any) -> EmbeddingModelRecord | None:
        if not raw:
            return None
        try:
            payload = json.loads(raw)
        except (TypeError, json.JSONDecodeError):
            return None
        try:
            return EmbeddingModelRecord(**payload)
        except TypeError:
            return None

    @staticmethod
    def _to_record(model: EmbeddingModel) -> EmbeddingModelRecord:
        return EmbeddingModelRecord(
            id=str(model.id),
            provider=model.provider.value if hasattr(model.provider, "value") else str(model.provider),
            model_name=str(model.model_name),
            dimensions=int(model.dimensions),
            qdrant_collection=str(model.qdrant_collection),
            is_active=bool(model.is_active),
            deprecated_at=model.deprecated_at.isoformat() if model.deprecated_at else None,
            created_at=model.created_at.isoformat() if model.created_at else datetime.now(UTC).isoformat(),
        )

    @staticmethod
    def _default_model_record() -> EmbeddingModelRecord:
        settings = get_settings()
        provider_name = (os.getenv("EMBEDDING_PROVIDER") or settings.embedding_provider or EmbeddingProvider.openai.value).strip().lower()
        try:
            provider = EmbeddingProvider(provider_name).value
        except ValueError as error:
            raise ValueError(f"Unsupported EMBEDDING_PROVIDER '{provider_name}'.") from error

        default_model = "text-embedding-3-small"
        return EmbeddingModelRecord(
            id=os.getenv("EMBEDDING_MODEL_ID") or settings.embedding_model_id or DEFAULT_ACTIVE_MODEL_ID,
            provider=provider,
            model_name=os.getenv("EMBEDDING_MODEL") or settings.embedding_model or default_model,
            dimensions=int(os.getenv("EMBEDDING_DIMENSIONS") or settings.embedding_dimensions or "1536"),
            qdrant_collection=os.getenv("QDRANT_COLLECTION") or settings.qdrant_collection or "memories",
            is_active=True,
            deprecated_at=None,
            created_at=datetime.now(UTC).isoformat(),
        )

    @staticmethod
    def _env_model_overrides_active_model() -> bool:
        return any(
            os.getenv(name)
            for name in (
                "EMBEDDING_PROVIDER",
                "EMBEDDING_MODEL",
                "EMBEDDING_MODEL_ID",
                "EMBEDDING_DIMENSIONS",
                "QDRANT_COLLECTION",
            )
        )

    @staticmethod
    def _is_embedding_model_like(model: Any) -> bool:
        required_fields = (
            "id",
            "provider",
            "model_name",
            "dimensions",
            "qdrant_collection",
            "is_active",
            "deprecated_at",
            "created_at",
        )
        return all(hasattr(model, field) for field in required_fields)


def embed_sync_text(
    text: str,
    *,
    sync_session: Session | None = None,
    model_id: str | None = None,
) -> EmbeddingResult:
    service = EmbeddingService(sync_session=sync_session)
    return service.embed_sync(text, model_id=model_id)


async def embed_text(
    text: str,
    *,
    async_session: AsyncSession | None = None,
    model_id: str | None = None,
) -> EmbeddingResult:
    service = EmbeddingService(async_session=async_session)
    return await service.embed(text, model_id=model_id)
