from __future__ import annotations

import asyncio
import inspect
from typing import Any

from google import genai
from google.genai import types

from api.infra.llm_providers.base import LLMProvider
from api.infra.secrets import resolve_api_key


DEFAULT_GEMINI_EMBED_MODEL = "gemini-embedding-001"
DEFAULT_GEMINI_EXTRACT_MODEL = "gemini-2.5-flash-lite"
DEFAULT_GEMINI_DIMENSIONS = 1536


class GeminiProvider(LLMProvider):
    provider_name = "gemini"

    def __init__(
        self,
        *,
        client: Any | None = None,
        embed_model: str = DEFAULT_GEMINI_EMBED_MODEL,
        extract_model: str = DEFAULT_GEMINI_EXTRACT_MODEL,
        embedding_dimensions: int = DEFAULT_GEMINI_DIMENSIONS,
        api_key: str | None = None,
    ) -> None:
        super().__init__()
        self.embed_model = embed_model
        self.extract_model = extract_model
        self.embedding_dimensions = embedding_dimensions
        resolved_api_key = api_key or resolve_api_key(
            secret_name_env="GEMINI_API_KEY_SECRET_NAME",
            direct_value_env="GEMINI_API_KEY",
        )
        self.client = client
        self.api_key = resolved_api_key

    def embed(self, text: str) -> list[float]:
        response = self._run_maybe_awaitable(
            self._embed_models_client.embed_content(
            model=self.embed_model,
            contents=text,
            config=types.EmbedContentConfig(output_dimensionality=self.embedding_dimensions),
            )
        )
        embeddings = getattr(response, "embeddings", None) or []
        if not embeddings:
            raise ValueError("Gemini embedding response did not contain embeddings.")
        values = getattr(embeddings[0], "values", None) or []
        return [float(value) for value in values]

    def extract(self, messages: list[dict], system_prompt: str) -> str:
        prompt = self._messages_to_text(messages)
        response = self._run_maybe_awaitable(
            self._generate_models_client.generate_content(
            model=self.extract_model,
            contents=prompt,
            config=types.GenerateContentConfig(
                system_instruction=system_prompt,
                response_mime_type="application/json",
            ),
            )
        )
        return str(getattr(response, "text", "") or "{}")

    def _check_availability(self) -> bool:
        if self.client is not None:
            return True
        return bool(self.api_key)

    @property
    def _client(self):
        if self.client is None:
            self.client = genai.Client(api_key=self.api_key)
        return self.client

    @property
    def _embed_models_client(self):
        client = self._client
        models = getattr(client, "models", None)
        if models is not None and getattr(models, "embed_content", None) is not None:
            return models
        aio = getattr(client, "aio", None)
        if aio is not None and getattr(aio, "models", None) is not None:
            return aio.models
        return models

    @property
    def _generate_models_client(self):
        return self._client.models

    @staticmethod
    def _run_maybe_awaitable(value: Any) -> Any:
        if not inspect.isawaitable(value):
            return value
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(value)

        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(value)
        finally:
            loop.close()

    @staticmethod
    def _messages_to_text(messages: list[dict]) -> str:
        return "\n".join(
            f"{str(message.get('role', 'user')).upper()}: {str(message.get('content', '')).strip()}"
            for message in messages
        )


__all__ = ["GeminiProvider"]
