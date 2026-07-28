from __future__ import annotations

from typing import Any

import httpx

from api.infra.llm_providers.base import LLMProvider
from api.infra.secrets import resolve_api_key


DEFAULT_OPENAI_EMBED_MODEL = "text-embedding-3-small"
DEFAULT_OPENAI_EXTRACT_MODEL = "gpt-4o-mini"
DEFAULT_OPENAI_DIMENSIONS = 1536


class OpenAIProvider(LLMProvider):
    provider_name = "openai"

    def __init__(
        self,
        *,
        http_client: httpx.Client | None = None,
        embed_model: str = DEFAULT_OPENAI_EMBED_MODEL,
        extract_model: str = DEFAULT_OPENAI_EXTRACT_MODEL,
        embedding_dimensions: int = DEFAULT_OPENAI_DIMENSIONS,
        api_key: str | None = None,
    ) -> None:
        super().__init__()
        self.http_client = http_client or httpx.Client(timeout=30.0)
        self.embed_model = embed_model
        self.extract_model = extract_model
        self.embedding_dimensions = embedding_dimensions
        self.api_key = api_key or resolve_api_key(
            secret_name_env="OPENAI_API_KEY_SECRET_NAME",
            direct_value_env="OPENAI_API_KEY",
        )

    def embed(self, text: str) -> list[float]:
        payload: dict[str, object] = {
            "model": self.embed_model,
            "input": text,
        }
        if self.embedding_dimensions > 0:
            payload["dimensions"] = self.embedding_dimensions

        response = self.http_client.post(
            "https://api.openai.com/v1/embeddings",
            headers={
                "Authorization": f"Bearer {self.api_key or ''}",
                "content-type": "application/json",
            },
            json=payload,
        )
        response.raise_for_status()
        response_payload: Any = response.json()
        data = response_payload.get("data") or []
        if data and isinstance(data[0], dict):
            embedding = data[0].get("embedding") or []
            return [float(value) for value in embedding]
        raise ValueError("OpenAI embedding response did not contain embeddings.")

    def extract(self, messages: list[dict], system_prompt: str) -> str:
        response = self.http_client.post(
            "https://api.openai.com/v1/chat/completions",
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
        response_payload: Any = response.json()
        choices = response_payload.get("choices") or []
        if choices and isinstance(choices[0], dict):
            message = choices[0].get("message") or {}
            return str(message.get("content") or "{}")
        return "{}"

    def _check_availability(self) -> bool:
        return bool(self.api_key)


__all__ = ["OpenAIProvider"]
