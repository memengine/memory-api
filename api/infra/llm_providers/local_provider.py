from __future__ import annotations

import os
from typing import Any

import httpx

from api.infra.llm_providers.base import LLMProvider


DEFAULT_LOCAL_EMBEDDING_ENDPOINT = "http://localhost:11434/api/embeddings"


class LocalProvider(LLMProvider):
    provider_name = "local"
    supports_extraction = False

    def __init__(
        self,
        *,
        http_client: httpx.Client | None = None,
        endpoint: str | None = None,
        model_name: str = "local-embeddings",
        embedding_dimensions: int = 384,
    ) -> None:
        super().__init__()
        self.http_client = http_client or httpx.Client(timeout=10.0)
        self.endpoint = endpoint or os.getenv("LOCAL_EMBEDDING_ENDPOINT") or DEFAULT_LOCAL_EMBEDDING_ENDPOINT
        self.model_name = model_name
        self.embedding_dimensions = embedding_dimensions

    def embed(self, text: str) -> list[float]:
        response = self.http_client.post(
            self.endpoint,
            json={
                "model": self.model_name,
                "text": text,
                "dimensions": self.embedding_dimensions,
            },
        )
        response.raise_for_status()
        payload: Any = response.json()
        values = payload.get("embedding") or payload.get("vector")
        if not isinstance(values, list):
            raise ValueError("Local embedding endpoint did not return a vector.")
        return [float(value) for value in values]

    def extract(self, messages: list[dict], system_prompt: str) -> str:
        raise NotImplementedError("Local provider does not support extraction.")

    def _check_availability(self) -> bool:
        return bool(self.endpoint)


__all__ = ["LocalProvider"]
