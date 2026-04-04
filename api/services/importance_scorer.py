from __future__ import annotations

from datetime import UTC
from datetime import datetime
from typing import Any

from api.db.models import Memory
from api.db.models import MemoryCategory
from api.schemas.memory_schemas import ExtractedMemory


CATEGORY_WEIGHTS: dict[str, float] = {
    MemoryCategory.goal.value: 1.5,
    MemoryCategory.procedure.value: 1.0,
    MemoryCategory.preference.value: 0.5,
    MemoryCategory.fact.value: 0.0,
    MemoryCategory.relationship.value: 0.8,
    MemoryCategory.expertise.value: 0.3,
}
MAX_ACCESS_BOOST = 0.5
ACCESS_BOOST_CAP_COUNT = 100


class ImportanceScorer:
    def __init__(
        self,
        *,
        category_weights: dict[str, float] | None = None,
        max_access_boost: float = MAX_ACCESS_BOOST,
        access_boost_cap_count: int = ACCESS_BOOST_CAP_COUNT,
    ) -> None:
        self.category_weights = category_weights or dict(CATEGORY_WEIGHTS)
        self.max_access_boost = max_access_boost
        self.access_boost_cap_count = access_boost_cap_count

    def score(self, memory: ExtractedMemory, user_context: dict[str, Any] | None = None) -> float:
        llm_score = float(memory.importance_score)
        category_weight = self._category_weight(memory.category)
        access_boost = self.calculate_access_pattern_boost(user_context or {})
        return self.normalize_score(llm_score + category_weight + access_boost)

    def calculate_access_pattern_boost(self, user_context: dict[str, Any]) -> float:
        similar_access_count = self._extract_similar_access_count(user_context)
        return self._access_boost_for_count(similar_access_count)

    def record_access(self, memory: Memory) -> float:
        previous_count = int(memory.access_count or 0)
        new_count = previous_count + 1
        previous_boost = self._access_boost_for_count(previous_count)
        new_boost = self._access_boost_for_count(new_count)

        memory.access_count = new_count
        memory.last_accessed_at = datetime.now(UTC)
        memory.importance_score = self.normalize_score(
            round(float(memory.importance_score) + (new_boost - previous_boost), 6)
        )
        return memory.importance_score

    def increment_access(self, memory: Memory) -> float:
        return self.record_access(memory)

    @staticmethod
    def normalize_score(score: float) -> float:
        return max(1.0, min(10.0, float(score)))

    def _category_weight(self, category: str) -> float:
        return float(self.category_weights.get(str(category).lower(), 0.0))

    def _access_boost_for_count(self, access_count: int) -> float:
        bounded_count = max(0, min(int(access_count), self.access_boost_cap_count))
        return (bounded_count / self.access_boost_cap_count) * self.max_access_boost

    @staticmethod
    def _extract_similar_access_count(user_context: dict[str, Any]) -> int:
        if "similar_access_count" in user_context:
            return int(user_context["similar_access_count"] or 0)

        if "similar_memory_access_count" in user_context:
            return int(user_context["similar_memory_access_count"] or 0)

        similar_memories = user_context.get("similar_memories", [])
        if isinstance(similar_memories, list):
            total = 0
            for memory in similar_memories:
                if isinstance(memory, dict):
                    total += int(memory.get("access_count", 0) or 0)
                else:
                    total += int(getattr(memory, "access_count", 0) or 0)
            return total

        return 0
