from __future__ import annotations

from typing import Any

import httpx

from api.infra.llm_providers.base import LLMProvider
from api.infra.secrets import resolve_api_key


DEFAULT_COHERE_EMBED_MODEL = "embed-english-v3.0"
DEFAULT_COHERE_EXTRACT_MODEL = "command"
DEFAULT_COHERE_DIMENSIONS = 1024


class CohereProvider(LLMProvider):
    provider_name = "cohere"

    def __init__(
        self,
        *,
        http_client: httpx.Client | None = None,
        embed_model: str = DEFAULT_COHERE_EMBED_MODEL,
        extract_model: str = DEFAULT_COHERE_EXTRACT_MODEL,
        embedding_dimensions: int = DEFAULT_COHERE_DIMENSIONS,
        api_key: str | None = None,
    ) -> None:
        super().__init__()
        self.http_client = http_client or httpx.Client(timeout=10.0)
        self.embed_model = embed_model
        self.extract_model = extract_model
        self.embedding_dimensions = embedding_dimensions
        self.api_key = api_key or resolve_api_key(
            secret_name_env="COHERE_API_KEY_SECRET_NAME",
            direct_value_env="COHERE_API_KEY",
        )

    def embed(self, text: str) -> list[float]:
        response = self.http_client.post(
            "https://api.cohere.com/v2/embed",
            headers={
                "Authorization": f"Bearer {self.api_key or ''}",
                "content-type": "application/json",
            },
            json={
                "model": self.embed_model,
                "input_type": "search_document",
                "texts": [text],
            },
        )
        response.raise_for_status()
        payload: Any = response.json()
        embeddings = payload.get("embeddings") or {}
        if isinstance(embeddings, dict):
            float_values = embeddings.get("float") or []
            if float_values:
                return [float(value) for value in float_values[0]]
        raise ValueError("Cohere embedding response did not contain embeddings.")

    def extract(self, messages: list[dict], system_prompt: str) -> str:
        response = self.http_client.post(
            "https://api.cohere.com/v2/chat",
            headers={
                "Authorization": f"Bearer {self.api_key or ''}",
                "content-type": "application/json",
            },
            json={
                "model": self.extract_model,
                "messages": [{"role": "system", "content": system_prompt}, *messages],
                "response_format": {"type": "json_object"},
            },
        )
        response.raise_for_status()
        payload: Any = response.json()
        message = payload.get("message") or {}
        content = message.get("content") or []
        if isinstance(content, list) and content:
            first = content[0]
            if isinstance(first, dict):
                return str(first.get("text", "") or "{}")
        text_value = payload.get("text")
        return str(text_value or "{}")

    def _check_availability(self) -> bool:
        return bool(self.api_key)


__all__ = ["CohereProvider"]
