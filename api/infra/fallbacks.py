from __future__ import annotations

import logging
from typing import Any

from api.errors import APIError


LOGGER = logging.getLogger("memoryos.circuit_fallbacks")


class ServiceUnavailableError(APIError):
    def __init__(self, service_name: str) -> None:
        super().__init__(
            status_code=503,
            code="SRV_503",
            error="service_unavailable",
            details={"service": service_name},
        )


def on_redis_open(default: Any = None) -> Any:
    LOGGER.warning("Redis circuit open; falling back to cache miss / rate limit disabled mode.")
    return default


def on_qdrant_open() -> list[Any]:
    LOGGER.warning("Qdrant circuit open; returning empty vector search results.")
    return []


def on_gemini_embed_open() -> None:
    LOGGER.warning("Gemini embedding circuit open; returning degraded response without embeddings.")
    return None


def on_postgres_open() -> None:
    LOGGER.error("Postgres circuit open; raising service unavailable.")
    raise ServiceUnavailableError("postgres")
