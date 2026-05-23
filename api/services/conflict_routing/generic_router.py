from __future__ import annotations

from api.db.models import Memory
from api.services.conflict_routing.base import BaseEntityRouter
from api.services.conflict_routing.base import ResolutionPath


class GenericEntityRouter(BaseEntityRouter):
    """
    Fallback router when no domain schema is active.
    Uses content signals and ownership, never entity-name lists.
    """

    PERSONAL_SIGNALS = [
        "i ",
        "my ",
        "i've",
        "i have",
        "i prefer",
        "i use",
        "i like",
        "i need",
        "for me",
    ]

    SHARED_SIGNALS = [
        "we ",
        "our ",
        "the team",
        "the company",
        "everyone",
        "all of us",
        "the project",
        "our stack",
        "our process",
    ]

    def get_domain(self) -> str:
        return "generic"

    def classify(
        self,
        entity_type: str,
        memory_a: Memory,
        memory_b: Memory,
    ) -> ResolutionPath | None:
        del entity_type
        combined = f"{memory_a.content} {memory_b.content}".lower()
        personal_score = sum(1 for signal in self.PERSONAL_SIGNALS if signal in combined)
        shared_score = sum(1 for signal in self.SHARED_SIGNALS if signal in combined)

        if personal_score > shared_score:
            return "user_session"
        if shared_score > personal_score:
            return "tenant_review"
        if memory_a.proxy_user_id == memory_b.proxy_user_id:
            return "user_session"
        return "tenant_review"
