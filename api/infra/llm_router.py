from __future__ import annotations

import os
import json
import uuid
from dataclasses import asdict
from dataclasses import dataclass
from typing import Callable

import redis
from sqlalchemy import select
from sqlalchemy.orm import Session

from api.db.models import LLMProviderConfig
from api.infra.circuit_breaker_registry import CircuitBreakerRegistry
from api.infra.llm_providers.base import LLMProvider


def _gemini_provider(**overrides) -> LLMProvider:
    from api.infra.llm_providers.gemini_provider import GeminiProvider

    return GeminiProvider(**overrides)


def _anthropic_provider(**overrides) -> LLMProvider:
    from api.infra.llm_providers.anthropic_provider import AnthropicProvider

    return AnthropicProvider(**overrides)


def _cohere_provider(**overrides) -> LLMProvider:
    from api.infra.llm_providers.cohere_provider import CohereProvider

    return CohereProvider(**overrides)


def _local_provider(**overrides) -> LLMProvider:
    from api.infra.llm_providers.local_provider import LocalProvider

    return LocalProvider(**overrides)


def _openai_provider(**overrides) -> LLMProvider:
    from api.infra.llm_providers.openai_provider import OpenAIProvider

    return OpenAIProvider(**overrides)

class EmbeddingUnavailableError(RuntimeError):
    pass


class ExtractionUnavailableError(RuntimeError):
    pass


@dataclass(slots=True)
class ProviderConfigRecord:
    embed_provider_primary: str = "openai"
    embed_provider_fallback: str = ""
    extract_provider_primary: str = "openai"
    extract_provider_fallback: str = ""


class LLMRouter:
    HEALTH_CACHE_TTL_SECONDS = 60
    CONFIG_CACHE_TTL_SECONDS = 300

    def __init__(
        self,
        *,
        sync_session: Session | None = None,
        redis_client: redis.Redis | None = None,
        provider_factories: dict[str, Callable[..., LLMProvider]] | None = None,
    ) -> None:
        self.sync_session = sync_session
        self.redis_client = redis_client
        self.provider_factories = provider_factories or {
            "gemini": lambda **overrides: _gemini_provider(**overrides),
            "anthropic": lambda **overrides: _anthropic_provider(**overrides),
            "cohere": lambda **overrides: _cohere_provider(**overrides),
            "local": lambda **overrides: _local_provider(**overrides),
            "openai": lambda **overrides: _openai_provider(**overrides),
        }

    def get_provider(self, provider_name: str, **overrides) -> LLMProvider:
        factory = self.provider_factories.get(provider_name)
        if factory is None:
            raise ValueError(f"Unsupported LLM provider '{provider_name}'.")
        return factory(**overrides)

    def get_embed_provider(self, tenant_id: str | None = None) -> LLMProvider:
        config = self._provider_config(tenant_id)
        candidates = self._dedupe_provider_names(
            [
                config.embed_provider_primary,
                config.embed_provider_fallback,
            ]
        )
        for provider_name in candidates:
            provider = self.get_provider(provider_name)
            if not provider.supports_embeddings:
                continue
            if self._provider_available(provider, capability="embed"):
                return provider
        raise EmbeddingUnavailableError("No embedding provider is available.")

    def get_extract_provider(self, tenant_id: str | None = None) -> LLMProvider:
        config = self._provider_config(tenant_id)
        candidates = self._dedupe_provider_names(
            [
                config.extract_provider_primary,
                config.extract_provider_fallback,
            ]
        )
        for provider_name in candidates:
            provider = self.get_provider(provider_name)
            if not provider.supports_extraction:
                continue
            if self._provider_available(provider, capability="extract"):
                return provider
        raise ExtractionUnavailableError("No extraction provider is available.")

    def _provider_config(self, tenant_id: str | None) -> ProviderConfigRecord:
        env_config = self._env_provider_config()
        if env_config is not None:
            return env_config
        cache_key = self._config_cache_key(tenant_id)
        cached = self._get_cached(cache_key)
        if cached is not None:
            return ProviderConfigRecord(**cached)

        record = self._load_provider_config(tenant_id)
        self._set_cached(cache_key, asdict(record), self.CONFIG_CACHE_TTL_SECONDS)
        return record

    @staticmethod
    def _env_provider_config() -> ProviderConfigRecord | None:
        embed_provider = (os.getenv("EMBEDDING_PROVIDER") or "").strip().lower()
        raw_extract_order = (os.getenv("LLM_PROVIDER_ORDER") or "").strip()
        extract_providers = [name.strip().lower() for name in raw_extract_order.split(",") if name.strip()]
        extract_provider = extract_providers[0] if extract_providers else ""
        if not embed_provider and not extract_provider:
            return None
        return ProviderConfigRecord(
            embed_provider_primary=embed_provider or "openai",
            embed_provider_fallback="",
            extract_provider_primary=extract_provider or "openai",
            extract_provider_fallback="",
        )

    def _load_provider_config(self, tenant_id: str | None) -> ProviderConfigRecord:
        if self.sync_session is None:
            return ProviderConfigRecord()

        tenant_record = None
        if tenant_id:
            tenant_uuid = uuid.UUID(str(tenant_id))
            tenant_record = self.sync_session.execute(
                select(LLMProviderConfig).where(LLMProviderConfig.tenant_id == tenant_uuid).limit(1)
            ).scalar_one_or_none()
        if tenant_record is None:
            tenant_record = self.sync_session.execute(
                select(LLMProviderConfig).where(LLMProviderConfig.tenant_id.is_(None)).limit(1)
            ).scalar_one_or_none()
        if tenant_record is None:
            return ProviderConfigRecord()
        return ProviderConfigRecord(
            embed_provider_primary=str(tenant_record.embed_provider_primary),
            embed_provider_fallback=str(tenant_record.embed_provider_fallback),
            extract_provider_primary=str(tenant_record.extract_provider_primary),
            extract_provider_fallback=str(tenant_record.extract_provider_fallback),
        )

    def _provider_available(self, provider: LLMProvider, *, capability: str) -> bool:
        if self._breaker_open(provider.provider_name, capability=capability):
            return False

        cache_key = f"llm_provider_health:{capability}:{provider.provider_name}"
        cached = self._get_cached(cache_key)
        if isinstance(cached, dict) and cached.get("available") is True:
            return True

        available = bool(provider.is_available())
        if available:
            self._set_cached(
                cache_key,
                {"available": True},
                self.HEALTH_CACHE_TTL_SECONDS,
            )
        else:
            self._delete_cached(cache_key)
        return available

    @staticmethod
    def _dedupe_provider_names(provider_names: list[str]) -> list[str]:
        ordered: list[str] = []
        for provider_name in provider_names:
            normalized = str(provider_name or "").strip().lower()
            if not normalized or normalized in ordered:
                continue
            ordered.append(normalized)
        return ordered

    @staticmethod
    def _config_cache_key(tenant_id: str | None) -> str:
        return f"llm_provider_config:{tenant_id or 'global'}"

    def _get_cached(self, key: str):
        if self.redis_client is None:
            return None
        try:
            raw = self.redis_client.get(key)
        except Exception:
            return None
        if not raw:
            return None
        try:
            return json.loads(raw)
        except (TypeError, json.JSONDecodeError):
            return None

    def _set_cached(self, key: str, payload: dict[str, object], ttl_seconds: int) -> None:
        if self.redis_client is None:
            return
        try:
            self.redis_client.set(key, json.dumps(payload), ex=ttl_seconds)
        except Exception:
            return

    def _delete_cached(self, key: str) -> None:
        if self.redis_client is None:
            return
        try:
            self.redis_client.delete(key)
        except Exception:
            return

    @staticmethod
    def _breaker_open(provider_name: str, *, capability: str) -> bool:
        registry = CircuitBreakerRegistry.get_instance()
        if provider_name == "gemini":
            breaker = registry.gemini_embed_cb if capability == "embed" else registry.gemini_extract_cb
            return breaker.current_state() == "OPEN"
        return False


__all__ = [
    "EmbeddingUnavailableError",
    "ExtractionUnavailableError",
    "LLMRouter",
    "ProviderConfigRecord",
]
