from __future__ import annotations

from typing import Any

import httpx

from api.infra.llm_providers.base import LLMProvider
from api.infra.secrets import resolve_api_key


DEFAULT_ANTHROPIC_EXTRACT_MODEL = "claude-haiku-4-5-20251001"


class AnthropicProvider(LLMProvider):
    provider_name = "anthropic"
    embedding_dimensions = 0
    supports_embeddings = False

    def __init__(
        self,
        *,
        http_client: httpx.Client | None = None,
        extract_model: str = DEFAULT_ANTHROPIC_EXTRACT_MODEL,
        api_key: str | None = None,
    ) -> None:
        super().__init__()
        self.http_client = http_client or httpx.Client(timeout=10.0)
        self.extract_model = extract_model
        self.api_key = api_key or resolve_api_key(
            secret_name_env="ANTHROPIC_API_KEY_SECRET_NAME",
            direct_value_env="ANTHROPIC_API_KEY",
        )

    def embed(self, text: str) -> list[float]:
        raise NotImplementedError("Anthropic does not support embeddings.")

    def extract(self, messages: list[dict], system_prompt: str) -> str:
        response = self.http_client.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": str(self.api_key or ""),
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": self.extract_model,
                "system": system_prompt,
                "max_tokens": 2048,
                "messages": messages,
            },
        )
        response.raise_for_status()
        payload: Any = response.json()
        content = payload.get("content") or []
        if isinstance(content, list) and content:
            first = content[0]
            if isinstance(first, dict):
                return str(first.get("text", "") or "{}")
        return "{}"

    def _check_availability(self) -> bool:
        return bool(self.api_key)


__all__ = ["AnthropicProvider"]
