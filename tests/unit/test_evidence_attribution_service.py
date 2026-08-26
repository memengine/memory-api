from __future__ import annotations

import json

import pytest

from api.services.evidence_attribution_service import EvidenceAttributionService
from api.services.llm_service import LLMResponse


class FakeLLMService:
    def __init__(self, content: str) -> None:
        self.content = content
        self.calls: list[dict[str, object]] = []

    async def complete(self, **kwargs: object) -> LLMResponse:
        self.calls.append(kwargs)
        return LLMResponse(
            content=self.content,
            provider_used="test",
            model_used="test-model",
            input_tokens=10,
            output_tokens=5,
            total_tokens=15,
            latency_ms=7,
        )


@pytest.mark.asyncio
async def test_attribution_maps_turns_without_mutating_memories() -> None:
    llm = FakeLLMService(
        json.dumps({"attributions": [{"memory_id": 0, "evidence_turns": [2, 0, 2]}]})
    )
    service = EvidenceAttributionService(llm)  # type: ignore[arg-type]
    memories = [{"content": "User prefers Python", "category": "preference"}]
    original = [dict(memory) for memory in memories]

    result = await service.attribute(
        memories=memories,
        messages=[
            {"role": "user", "content": "I prefer Python"},
            {"role": "assistant", "content": "Okay"},
            {"role": "user", "content": "Python works best for me"},
        ],
    )

    assert result.evidence_by_memory == {0: [0, 2]}
    assert memories == original
    assert llm.calls[0]["temperature"] == 0.0


def test_attribution_parser_rejects_unknown_ids_and_invalid_turns() -> None:
    raw = json.dumps(
        {
            "attributions": [
                {"memory_id": 0, "evidence_turns": [-1, 0, 9, True]},
                {"memory_id": 4, "evidence_turns": [0]},
            ]
        }
    )

    assert EvidenceAttributionService._parse(raw, memory_count=1, turn_count=2) == {0: [0]}


@pytest.mark.asyncio
async def test_empty_memories_skip_provider_call() -> None:
    llm = FakeLLMService("{}")
    service = EvidenceAttributionService(llm)  # type: ignore[arg-type]

    result = await service.attribute(memories=[], messages=[])

    assert result.evidence_by_memory == {}
    assert result.response is None
    assert llm.calls == []
