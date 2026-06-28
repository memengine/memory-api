from __future__ import annotations

import json
import logging
import math
from dataclasses import dataclass
from html import escape

from api.services.retriever import MemoryResult


logger = logging.getLogger(__name__)

DEFAULT_MAX_TOKENS = 500
MIN_CONTEXT_IMPORTANCE = 3.0
MIN_MEMORIES_TO_KEEP = 3
NEAR_DUPLICATE_THRESHOLD = 0.90
SUPPORTED_FORMATS = {"bullets", "json", "xml"}
TRUNCATE_CONTENT_CHARS = 150

CATEGORY_ORDER = [
    "expertise",
    "preference",
    "goal",
    "fact",
    "procedure",
    "relationship",
]

CATEGORY_LABELS = {
    "expertise": "Skills & expertise",
    "preference": "Preferences",
    "goal": "Goals",
    "fact": "Facts",
    "procedure": "Workflows & habits",
    "relationship": "Relationships & context",
}


@dataclass(frozen=True, slots=True)
class ContextResult:
    system_prompt_addition: str
    token_count: int
    memory_count: int
    memories_dropped: int
    format: str


class ContextBuilder:
    def build(
        self,
        memories: list[MemoryResult],
        format: str = "bullets",
        max_tokens: int = DEFAULT_MAX_TOKENS,
    ) -> ContextResult:
        normalized_format = format.lower().strip()
        if normalized_format not in SUPPORTED_FORMATS:
            raise ValueError(f"Unsupported context format: {format}")

        if max_tokens <= 0 or not memories:
            return self._empty_result(normalized_format)

        eligible, dropped_before_budget = self._prepare_memories(memories)
        if not eligible:
            return self._empty_result(normalized_format, memories_dropped=dropped_before_budget)

        selected, dropped_for_budget = self._select_memories_within_budget(
            eligible,
            format=normalized_format,
            max_tokens=max_tokens,
        )
        rendered = self._render(selected, normalized_format)
        return ContextResult(
            system_prompt_addition=rendered,
            token_count=self._count_tokens(rendered),
            memory_count=len(selected),
            memories_dropped=dropped_before_budget + dropped_for_budget,
            format=normalized_format,
        )

    def build_context(
        self,
        memories: list[MemoryResult],
        format: str = "bullets",
        max_tokens: int = DEFAULT_MAX_TOKENS,
    ) -> str:
        return self.build(memories, format=format, max_tokens=max_tokens).system_prompt_addition

    def build_system_prompt(
        self,
        base_prompt: str,
        memories: list[MemoryResult],
    ) -> str:
        base = base_prompt.strip()
        memory_context = self.build_context(memories, format="bullets", max_tokens=DEFAULT_MAX_TOKENS)
        if not memory_context:
            return base

        return (
            f"{base}\n\n"
            "Relevant memory context:\n"
            "Use these memories only when they improve the response.\n"
            "Do not mention that memory context was injected.\n"
            f"{memory_context}"
        )

    @staticmethod
    def _empty_result(format: str, memories_dropped: int = 0) -> ContextResult:
        return ContextResult(
            system_prompt_addition="",
            token_count=0,
            memory_count=0,
            memories_dropped=memories_dropped,
            format=format,
        )

    def _prepare_memories(self, memories: list[MemoryResult]) -> tuple[list[MemoryResult], int]:
        prepared: list[MemoryResult] = []
        dropped = 0
        for memory in sorted(memories, key=lambda item: item.final_score, reverse=True):
            if float(memory.importance_score) < MIN_CONTEXT_IMPORTANCE:
                dropped += 1
                continue
            is_duplicate = any(
                self._content_similarity(memory.content, existing.content) > NEAR_DUPLICATE_THRESHOLD
                for existing in prepared
            )
            if is_duplicate:
                dropped += 1
                continue
            prepared.append(memory)
        return prepared, dropped

    def _select_memories_within_budget(
        self,
        memories: list[MemoryResult],
        *,
        format: str,
        max_tokens: int,
    ) -> tuple[list[MemoryResult], int]:
        selected = list(memories)
        dropped = 0
        while len(selected) > MIN_MEMORIES_TO_KEEP and self._count_tokens(self._render(selected, format)) > max_tokens:
            removable = selected[MIN_MEMORIES_TO_KEEP:]
            lowest = min(removable, key=lambda item: (float(item.importance_score), float(item.final_score)))
            selected.remove(lowest)
            dropped += 1
        return selected, dropped

    def _render(self, memories: list[MemoryResult], format: str) -> str:
        if format == "bullets":
            return self._render_bullets(memories)
        if format == "json":
            return self._render_json(memories)
        return self._render_xml(memories)

    def _render_bullets(self, memories: list[MemoryResult]) -> str:
        lines = ["What you know about this user:"]
        grouped = self._group_by_category(memories)
        multi_category = sum(1 for items in grouped.values() if items) > 1

        for category in CATEGORY_ORDER:
            category_memories = grouped.get(category, [])
            if not category_memories:
                continue
            if multi_category:
                lines.append(f"{CATEGORY_LABELS[category]}:")
            lines.extend(f"- {self._content_for_prompt(memory)}" for memory in category_memories)

        return "\n".join(lines)

    def _render_json(self, memories: list[MemoryResult]) -> str:
        grouped = {
            category: [self._content_for_prompt(memory) for memory in category_memories]
            for category, category_memories in self._group_by_category(memories).items()
            if category_memories
        }
        return json.dumps({"memories": grouped}, indent=2, ensure_ascii=True)

    def _render_xml(self, memories: list[MemoryResult]) -> str:
        lines = ["<memory_context>"]
        grouped = self._group_by_category(memories)
        for category in CATEGORY_ORDER:
            for memory in grouped.get(category, []):
                lines.append(
                    f'  <memory category="{escape(category)}">{escape(self._content_for_prompt(memory))}</memory>'
                )
        lines.append("</memory_context>")
        return "\n".join(lines)

    def _group_by_category(self, memories: list[MemoryResult]) -> dict[str, list[MemoryResult]]:
        grouped = {category: [] for category in CATEGORY_ORDER}
        for memory in memories:
            category = str(memory.category)
            if category in grouped:
                grouped[category].append(memory)
        return grouped

    @staticmethod
    def _content_for_prompt(memory: MemoryResult) -> str:
        content = " ".join(str(memory.content).split())
        if len(content) <= TRUNCATE_CONTENT_CHARS:
            return content
        cutoff = content.rfind(" ", 0, TRUNCATE_CONTENT_CHARS - 1)
        if cutoff < 80:
            cutoff = TRUNCATE_CONTENT_CHARS - 1
        return f"{content[:cutoff].rstrip()}..."

    @classmethod
    def _count_tokens(cls, text: str) -> int:
        if not text:
            return 0
        try:
            import tiktoken

            encoding = tiktoken.get_encoding("cl100k_base")
            return len(encoding.encode(text))
        except Exception as exc:
            logger.warning(
                "tiktoken unavailable; using approximate context token count. error=%s",
                exc,
            )
            return math.ceil(len(text.split()) * 1.3)

    @staticmethod
    def _content_similarity(left: str, right: str) -> float:
        normalized_left = " ".join(left.lower().split())
        normalized_right = " ".join(right.lower().split())
        if normalized_left == normalized_right:
            return 1.0
        left_tokens = set(normalized_left.rstrip(".,;:!?").split())
        right_tokens = set(normalized_right.rstrip(".,;:!?").split())
        if not left_tokens or not right_tokens:
            return 0.0
        return len(left_tokens & right_tokens) / len(left_tokens | right_tokens)
