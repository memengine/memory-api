from __future__ import annotations

import time
from abc import ABC
from abc import abstractmethod


class LLMProvider(ABC):
    provider_name: str = "unknown"
    embedding_dimensions: int = 0
    supports_embeddings: bool = True
    supports_extraction: bool = True

    def __init__(self) -> None:
        self._availability_checked_at = 0.0
        self._availability_value = False

    def is_available(self) -> bool:
        now = time.monotonic()
        if now - self._availability_checked_at < 30:
            return self._availability_value

        try:
            available = bool(self._check_availability())
        except Exception:
            available = False

        self._availability_checked_at = now
        self._availability_value = available
        return available

    @abstractmethod
    def embed(self, text: str) -> list[float]:
        raise NotImplementedError

    @abstractmethod
    def extract(self, messages: list[dict], system_prompt: str) -> str:
        raise NotImplementedError

    @abstractmethod
    def _check_availability(self) -> bool:
        raise NotImplementedError


__all__ = ["LLMProvider"]
