from __future__ import annotations

from abc import ABC
from abc import abstractmethod
from typing import Literal

from api.db.models import Memory

ResolutionPath = Literal["user_session", "tenant_review"]


class BaseEntityRouter(ABC):
    @abstractmethod
    def classify(
        self,
        entity_type: str,
        memory_a: Memory,
        memory_b: Memory,
    ) -> ResolutionPath | None:
        """
        Returns "user_session", "tenant_review", or None.
        None means this router does not know this entity and the general engine
        should fall back to the generic heuristic.
        """

    @abstractmethod
    def get_domain(self) -> str:
        """Return the domain slug this router handles."""
