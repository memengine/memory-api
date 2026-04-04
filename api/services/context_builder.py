from __future__ import annotations

import json
import math
from dataclasses import asdict
from html import escape
from typing import Any

from api.services.retriever import MemoryResult


DEFAULT_MAX_TOKENS = 800
SUPPORTED_FORMATS = {"bullets", "json", "xml"}


class ContextBuilder:
    def build_context(
        self,
        memories: list[MemoryResult],
        format: str = "bullets",
        max_tokens: int = DEFAULT_MAX_TOKENS,
    ) -> str:
        normalized_format = format.lower().strip()
        if normalized_format not in SUPPORTED_FORMATS:
            raise ValueError(f"Unsupported context format: {format}")

        if max_tokens <= 0 or not memories:
            return ""

        selected_memories = self._select_memories_within_budget(
            memories=memories,
            format=normalized_format,
            max_tokens=max_tokens,
        )
        if not selected_memories:
            return ""

        return self._render(selected_memories, normalized_format)

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

    def _select_memories_within_budget(
        self,
        *,
        memories: list[MemoryResult],
        format: str,
        max_tokens: int,
    ) -> list[MemoryResult]:
        selected = sorted(memories, key=lambda item: item.final_score, reverse=True)
        while selected:
            rendered = self._render(selected, format)
            if self._estimate_tokens(rendered) <= max_tokens:
                return selected
            selected.pop()
        return []

    def _render(self, memories: list[MemoryResult], format: str) -> str:
        if format == "bullets":
            return self._render_bullets(memories)
        if format == "json":
            return self._render_json(memories)
        return self._render_xml(memories)

    def _render_bullets(self, memories: list[MemoryResult]) -> str:
        return "\n".join(
            (
                f"- {memory.content} "
                f"(category: {memory.category}; "
                f"confidence: {self._confidence_label(memory.confidence_score)} "
                f"[{memory.confidence_score:.2f}]; "
                f"learned: {self._learned_at(memory)}; "
                f"importance: {memory.importance_score:.1f})"
            )
            for memory in memories
        )

    def _render_json(self, memories: list[MemoryResult]) -> str:
        payload = [self._memory_payload(memory) for memory in memories]
        return json.dumps(payload, indent=2, ensure_ascii=True)

    def _render_xml(self, memories: list[MemoryResult]) -> str:
        lines = ["<memory_context>"]
        for memory in memories:
            payload = self._memory_payload(memory)
            lines.extend(
                [
                    "  <memory>",
                    f"    <content>{escape(str(payload['content']))}</content>",
                    f"    <category>{escape(str(payload['category']))}</category>",
                    f"    <confidence level=\"{escape(str(payload['confidence_level']))}\">{payload['confidence_score']:.2f}</confidence>",
                    f"    <learned_at>{escape(str(payload['learned_at']))}</learned_at>",
                    f"    <importance_score>{payload['importance_score']:.1f}</importance_score>",
                    f"    <final_score>{payload['final_score']:.6f}</final_score>",
                    "  </memory>",
                ]
            )
        lines.append("</memory_context>")
        return "\n".join(lines)

    @staticmethod
    def _estimate_tokens(text: str) -> int:
        return math.ceil(len(text) / 4)

    def _memory_payload(self, memory: MemoryResult) -> dict[str, Any]:
        payload = asdict(memory)
        payload["confidence_level"] = self._confidence_label(memory.confidence_score)
        payload["learned_at"] = self._learned_at(memory)
        return payload

    @staticmethod
    def _learned_at(memory: MemoryResult) -> str:
        return memory.created_at or "unknown"

    @staticmethod
    def _confidence_label(score: float) -> str:
        if score >= 0.85:
            return "high"
        if score >= 0.65:
            return "medium"
        return "low"
