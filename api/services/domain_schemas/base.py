from __future__ import annotations

from abc import ABC
from abc import abstractmethod
from typing import Any


class BaseDomainSchema(ABC):
    """Interface for domain-specific memory overlays.

    The general memory engine remains authoritative for generic storage and
    cross-agent compatibility. Domain schemas add structured extraction and
    optional retrieval context on top.
    """

    @abstractmethod
    def get_domain(self) -> str:
        """Return the domain slug, for example: edtech, healthcare."""

    def extract_overlay_sync(
        self,
        *,
        session: Any,
        messages: list[dict[str, Any]],
        proxy_user_id: str,
        tenant_id: str,
        job_id: str,
        agent_id: str | None = None,
        client: Any | None = None,
    ) -> dict[str, Any] | None:
        """Run structured domain extraction after generic extraction.

        Return metadata to merge into the extraction job result. Return None
        when the schema has no extraction overlay.
        """
        return None

    async def build_retrieve_context(
        self,
        *,
        session: Any,
        cache_service: Any | None,
        proxy_user_id: str,
        tenant_id: str,
        query: str | None,
        max_tokens: int,
    ) -> tuple[str, int]:
        """Return extra domain-aware prompt context and token count."""
        return "", 0


__all__ = ["BaseDomainSchema"]
