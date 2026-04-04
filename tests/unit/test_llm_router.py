from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from unittest.mock import MagicMock

import pytest

from api.infra.llm_providers.base import LLMProvider
from api.infra.llm_router import EmbeddingUnavailableError
from api.infra.llm_router import ExtractionUnavailableError
from api.infra.llm_router import LLMRouter


class FakeRedis:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}

    def get(self, key: str):
        return self.values.get(key)

    def set(self, key: str, value: str, ex: int | None = None):
        self.values[key] = value
        return True


class FakeExecuteResult:
    def __init__(self, scalar=None) -> None:
        self._scalar = scalar

    def scalar_one_or_none(self):
        return self._scalar


@dataclass
class FakeConfig:
    tenant_id: uuid.UUID | None
    embed_provider_primary: str
    embed_provider_fallback: str
    extract_provider_primary: str
    extract_provider_fallback: str


class FakeProvider(LLMProvider):
    def __init__(
        self,
        *,
        provider_name: str,
        available: bool = True,
        supports_embeddings: bool = True,
        supports_extraction: bool = True,
        embedding_dimensions: int = 0,
    ) -> None:
        super().__init__()
        self.provider_name = provider_name
        self._available = available
        self.supports_embeddings = supports_embeddings
        self.supports_extraction = supports_extraction
        self.embedding_dimensions = embedding_dimensions

    def embed(self, text: str) -> list[float]:
        return [1.0]

    def extract(self, messages: list[dict], system_prompt: str) -> str:
        return "{}"

    def _check_availability(self) -> bool:
        return self._available


def test_get_extract_provider_falls_back_to_anthropic_when_gemini_unavailable() -> None:
    router = LLMRouter(
        redis_client=FakeRedis(),
        provider_factories={
            "gemini": lambda **kwargs: FakeProvider(provider_name="gemini", available=False),
            "anthropic": lambda **kwargs: FakeProvider(provider_name="anthropic", available=True),
        },
    )

    provider = router.get_extract_provider()

    assert provider.provider_name == "anthropic"


def test_get_embed_provider_falls_back_to_cohere_when_gemini_unavailable() -> None:
    router = LLMRouter(
        redis_client=FakeRedis(),
        provider_factories={
            "gemini": lambda **kwargs: FakeProvider(provider_name="gemini", available=False, embedding_dimensions=1536),
            "anthropic": lambda **kwargs: FakeProvider(provider_name="anthropic", available=True, supports_embeddings=False),
            "cohere": lambda **kwargs: FakeProvider(provider_name="cohere", available=True, embedding_dimensions=1024),
        },
    )

    provider = router.get_embed_provider()

    assert provider.provider_name == "cohere"


def test_tenant_specific_config_overrides_global_defaults() -> None:
    tenant_id = uuid.uuid4()
    session = MagicMock()
    session.execute.side_effect = [
        FakeExecuteResult(
            scalar=FakeConfig(
                tenant_id=tenant_id,
                embed_provider_primary="cohere",
                embed_provider_fallback="gemini",
                extract_provider_primary="anthropic",
                extract_provider_fallback="gemini",
            )
        )
    ]
    router = LLMRouter(
        sync_session=session,
        redis_client=FakeRedis(),
        provider_factories={
            "gemini": lambda **kwargs: FakeProvider(provider_name="gemini", available=True, embedding_dimensions=1536),
            "anthropic": lambda **kwargs: FakeProvider(provider_name="anthropic", available=True),
            "cohere": lambda **kwargs: FakeProvider(provider_name="cohere", available=True, embedding_dimensions=1024),
        },
    )

    embed_provider = router.get_embed_provider(str(tenant_id))
    extract_provider = router.get_extract_provider(str(tenant_id))

    assert embed_provider.provider_name == "cohere"
    assert extract_provider.provider_name == "anthropic"


def test_health_cache_prevents_repeat_availability_checks() -> None:
    redis_client = FakeRedis()
    provider = FakeProvider(provider_name="gemini", available=True, embedding_dimensions=1536)
    provider._check_availability = MagicMock(return_value=True)
    router = LLMRouter(
        redis_client=redis_client,
        provider_factories={"gemini": lambda **kwargs: provider},
    )

    first = router.get_embed_provider()
    second = router.get_embed_provider()

    assert first.provider_name == "gemini"
    assert second.provider_name == "gemini"
    assert provider._check_availability.call_count == 1
    assert json.loads(redis_client.values["llm_provider_health:embed:gemini"])["available"] is True


def test_extract_provider_raises_when_none_available() -> None:
    router = LLMRouter(
        redis_client=FakeRedis(),
        provider_factories={
            "gemini": lambda **kwargs: FakeProvider(provider_name="gemini", available=False),
            "anthropic": lambda **kwargs: FakeProvider(provider_name="anthropic", available=False),
        },
    )

    with pytest.raises(ExtractionUnavailableError):
        router.get_extract_provider()


def test_embed_provider_raises_when_none_available() -> None:
    router = LLMRouter(
        redis_client=FakeRedis(),
        provider_factories={
            "gemini": lambda **kwargs: FakeProvider(provider_name="gemini", available=False),
            "anthropic": lambda **kwargs: FakeProvider(provider_name="anthropic", available=True, supports_embeddings=False),
            "cohere": lambda **kwargs: FakeProvider(provider_name="cohere", available=False),
        },
    )

    with pytest.raises(EmbeddingUnavailableError):
        router.get_embed_provider()
