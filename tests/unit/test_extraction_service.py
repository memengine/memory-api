from __future__ import annotations

import json
from pathlib import Path

import pytest

from api.services.extraction_service import ExtractionError
from api.services.extraction_service import ExtractionService
from api.services.llm_service import LLMResponse


class FakeLLMService:
    def __init__(self, content: str) -> None:
        self.content = content
        self.calls: list[dict[str, object]] = []

    async def complete(self, **kwargs):
        self.calls.append(kwargs)
        return LLMResponse(
            content=self.content,
            provider_used="test",
            model_used="fake",
            input_tokens=11,
            output_tokens=7,
            total_tokens=18,
            latency_ms=1,
        )


def _spec(tmp_path: Path) -> Path:
    path = tmp_path / "extraction_spec.md"
    path.write_text(
        """
# MemoryOS extraction spec

## 1. Memory Categories

### PREFERENCE
**Definition:** User choices about communication style.
---
### FACT
**Definition:** Stable facts about the user.
---
### GOAL
**Definition:** Future outcomes the user wants.
---
### PROCEDURE
**Definition:** Workflows and habits.
---
### RELATIONSHIP
**Definition:** People and roles around the user.
---
### EXPERTISE
**Definition:** Skills, technologies, and knowledge domains.
---

## 2. Importance Scoring Rubric
Score 1 is low value. Score 10 is foundational.

## 3. Example Conversations
Example: User prefers short answers.

## 4. What Should NEVER Be Stored
**Rule 1 - Secrets**
Never store passwords or API keys.
---
**Rule 2 - Greetings**
Never store greetings.

## 5. Edge Cases
        """,
        encoding="utf-8",
    )
    return path


@pytest.mark.asyncio
async def test_extract_filters_and_returns_result(tmp_path: Path) -> None:
    llm = FakeLLMService(
        json.dumps(
            {
                "memories": [
                    {
                        "content": "User prefers concise Python-first explanations",
                        "category": "preference",
                        "importance_score": 8.0,
                        "confidence": 0.92,
                        "reasoning": "The user directly stated the preference.",
                    },
                    {
                        "content": "too short",
                        "category": "fact",
                        "importance_score": 8.0,
                        "confidence": 0.9,
                        "reasoning": "Too short to keep.",
                    },
                    {
                        "content": "User likes temporary random noise",
                        "category": "preference",
                        "importance_score": 1.5,
                        "confidence": 0.9,
                        "reasoning": "Low importance.",
                    },
                ],
                "nothing_to_extract": False,
            }
        )
    )
    service = ExtractionService(llm_service=llm, spec_path=_spec(tmp_path))

    result = await service.extract(
        messages=[{"role": "user", "content": "Remember I prefer concise Python-first explanations."}],
        proxy_user_id="proxy-1",
        tenant_id="tenant-1",
        job_id="job-1",
    )

    assert result.memories_extracted == 1
    assert result.memories_filtered == 2
    assert result.tokens_used == 18
    assert result.provider_used == "test"
    assert result.memories_to_store[0].content == "User prefers concise Python-first explanations"
    assert llm.calls[0]["response_format"] == "json"
    assert "What Should NEVER" not in llm.calls[0]["system_prompt"]


@pytest.mark.asyncio
async def test_extract_nothing_to_extract(tmp_path: Path) -> None:
    service = ExtractionService(
        llm_service=FakeLLMService(
            json.dumps(
                {
                    "memories": [],
                    "nothing_to_extract": True,
                    "extraction_notes": "Only greeting",
                }
            )
        ),
        spec_path=_spec(tmp_path),
    )

    result = await service.extract(
        messages=[{"role": "user", "content": "Hi"}],
        proxy_user_id="proxy-1",
        tenant_id="tenant-1",
        job_id="job-2",
    )

    assert result.nothing_to_extract is True
    assert result.memories_extracted == 0
    assert result.memories_to_store == []


@pytest.mark.asyncio
async def test_invalid_json_raises_extraction_error(tmp_path: Path) -> None:
    service = ExtractionService(
        llm_service=FakeLLMService("not json"),
        spec_path=_spec(tmp_path),
    )

    with pytest.raises(ExtractionError):
        await service.extract(
            messages=[{"role": "user", "content": "I use FastAPI."}],
            proxy_user_id="proxy-1",
            tenant_id="tenant-1",
            job_id="job-3",
        )


def test_system_prompt_includes_schema_and_categories(tmp_path: Path) -> None:
    service = ExtractionService(
        llm_service=FakeLLMService('{"memories":[]}'),
        spec_path=_spec(tmp_path),
    )

    prompt = service._build_system_prompt()

    assert "memory extraction specialist" in prompt
    assert "preference|fact|goal|procedure|relationship|expertise" in prompt
    assert "Only extract memories with confidence >= 0.65" in prompt
