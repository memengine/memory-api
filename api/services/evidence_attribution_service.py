from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from api.services.llm_service import LLMResponse, LLMService


@dataclass(frozen=True, slots=True)
class EvidenceAttributionResult:
    evidence_by_memory: dict[int, list[int]]
    response: LLMResponse | None


class EvidenceAttributionService:
    """Attach source-turn IDs without changing extracted memory propositions."""

    def __init__(self, llm_service: LLMService) -> None:
        self.llm_service = llm_service

    async def attribute(
        self,
        *,
        memories: list[dict[str, Any]],
        messages: list[dict[str, Any]],
    ) -> EvidenceAttributionResult:
        if not memories:
            return EvidenceAttributionResult(evidence_by_memory={}, response=None)

        payload = {
            "memories": [
                {"memory_id": index, "content": str(memory.get("content") or "")}
                for index, memory in enumerate(memories)
            ],
            "turns": [
                {
                    "turn_id": index,
                    "role": str(message.get("role") or "user"),
                    "content": str(message.get("content") or ""),
                }
                for index, message in enumerate(messages)
            ],
        }
        response = await self.llm_service.complete(
            system_prompt=(
                "You are an evidence-attribution component. For each already extracted memory, "
                "identify every conversation turn that directly supports it. You must not rewrite, "
                "merge, split, add, remove, or reinterpret memories. Return JSON only with shape "
                '{"attributions":[{"memory_id":0,"evidence_turns":[0]}]}. '
                "Use only supplied memory_id and turn_id integers. Do not cite a turn merely because "
                "it is topically related; it must directly support the memory proposition."
            ),
            user_message=json.dumps(payload, ensure_ascii=False),
            temperature=0.0,
            max_tokens=800,
            response_format="json",
        )
        return EvidenceAttributionResult(
            evidence_by_memory=self._parse(
                response.content,
                memory_count=len(memories),
                turn_count=len(messages),
            ),
            response=response,
        )

    @staticmethod
    def _parse(raw_content: str, *, memory_count: int, turn_count: int) -> dict[int, list[int]]:
        try:
            payload = json.loads(raw_content or "{}")
        except json.JSONDecodeError:
            return {}
        rows = payload.get("attributions") if isinstance(payload, dict) else None
        if not isinstance(rows, list):
            return {}

        result: dict[int, list[int]] = {}
        for row in rows:
            if not isinstance(row, dict):
                continue
            memory_id = row.get("memory_id")
            turns = row.get("evidence_turns")
            if not isinstance(memory_id, int) or isinstance(memory_id, bool):
                continue
            if memory_id < 0 or memory_id >= memory_count or not isinstance(turns, list):
                continue
            result[memory_id] = sorted(
                {
                    turn_id
                    for turn_id in turns
                    if isinstance(turn_id, int)
                    and not isinstance(turn_id, bool)
                    and 0 <= turn_id < turn_count
                }
            )
        return result


__all__ = ["EvidenceAttributionResult", "EvidenceAttributionService"]
