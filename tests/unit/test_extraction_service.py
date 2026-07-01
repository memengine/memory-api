from __future__ import annotations

import json
from pathlib import Path

import pytest

from api.services.extraction_service import ExtractionError
from api.services.extraction_service import ExtractionService
from api.services.llm_service import LLMResponse


class FakeLLMService:
    def __init__(self, content: str | list[str]) -> None:
        self.contents = list(content) if isinstance(content, list) else [content]
        self.calls: list[dict[str, object]] = []

    async def complete(self, **kwargs):
        self.calls.append(kwargs)
        index = min(len(self.calls) - 1, len(self.contents) - 1)
        return LLMResponse(
            content=self.contents[index],
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




@pytest.mark.asyncio
async def test_extract_buffers_borderline_candidates(tmp_path: Path) -> None:
    llm = FakeLLMService(
        json.dumps(
            {
                "memories": [
                    {
                        "content": "User may prefer short replies for difficult topics",
                        "category": "preference",
                        "importance_score": 6.0,
                        "confidence": 0.58,
                        "reasoning": "The user stated a weak preference.",
                    },
                    {
                        "content": "User likes unsupported noisy context",
                        "category": "preference",
                        "importance_score": 6.0,
                        "confidence": 0.3,
                        "reasoning": "Below pending threshold.",
                    },
                ],
                "nothing_to_extract": False,
            }
        )
    )
    service = ExtractionService(llm_service=llm, spec_path=_spec(tmp_path))

    result = await service.extract(
        messages=[{"role": "user", "content": "Maybe keep replies short for hard topics."}],
        proxy_user_id="proxy-1",
        tenant_id="tenant-1",
        job_id="job-pending-1",
    )

    assert result.memories_extracted == 0
    assert result.pending_candidates_count == 1
    assert result.memories_filtered == 1
    assert result.pending_candidates[0].content == "User may prefer short replies for difficult topics"
    assert result.pending_candidates[0].confidence == 0.58



@pytest.mark.asyncio
async def test_temporary_debugging_flow_preference_is_filtered(tmp_path: Path) -> None:
    llm = FakeLLMService(
        json.dumps(
            {
                "memories": [
                    {
                        "content": "User prefers to continue with the current debugging flow and not change anything.",
                        "category": "preference",
                        "importance_score": 5.0,
                        "confidence": 0.88,
                        "reasoning": "The user asked to keep going with the same debugging flow in this session.",
                    }
                ],
                "nothing_to_extract": False,
            }
        )
    )
    service = ExtractionService(llm_service=llm, spec_path=_spec(tmp_path))

    result = await service.extract(
        messages=[
            {"role": "user", "content": "Can you explain this warning a bit more? I am reading logs."},
            {"role": "assistant", "content": "Sure, paste the relevant line."},
            {"role": "user", "content": "Okay, please keep going with the same debugging flow and don't change anything else."},
        ],
        proxy_user_id="proxy-1",
        tenant_id="tenant-1",
        job_id="job-temp-debug-filter",
    )

    assert result.memories_extracted == 0
    assert result.pending_candidates_count == 0
    assert result.memories_filtered == 1
def test_system_prompt_includes_schema_and_categories(tmp_path: Path) -> None:
    service = ExtractionService(
        llm_service=FakeLLMService('{"memories":[]}'),
        spec_path=_spec(tmp_path),
    )

    prompt = service._build_system_prompt()

    assert "memory extraction specialist" in prompt
    assert "preference|fact|goal|procedure|relationship|expertise" in prompt
    assert "Extract strong memories with confidence >= 0.65" in prompt
    assert "borderline candidates with confidence >= 0.45" in prompt


@pytest.mark.asyncio
async def test_explicit_service_event_enables_authoritative_observation_mode(
    tmp_path: Path,
) -> None:
    llm = FakeLLMService(
        json.dumps(
            {
                "memories": [
                    {
                        "content": "User's current subscription plan is Growth",
                        "category": "fact",
                        "importance_score": 7.0,
                        "confidence": 0.95,
                        "reasoning": "The registered billing service reported the current plan.",
                    }
                ],
                "nothing_to_extract": False,
            }
        )
    )
    service = ExtractionService(llm_service=llm, spec_path=_spec(tmp_path))

    result = await service.extract(
        messages=[
            {"role": "user", "content": "What subscription plan is this customer using?"},
            {
                "role": "assistant",
                "content": "The customer's current subscription plan is Growth.",
            },
        ],
        proxy_user_id="proxy-1",
        tenant_id="tenant-1",
        job_id="job-source-1",
        source_context={
            "service": "billing-service",
            "observed_at": "2026-06-14T10:00:00+00:00",
        },
    )

    assert result.memories_extracted == 1
    assert "AUTHENTICATED SERVICE EVENT MODE" in llm.calls[0]["system_prompt"]
    assert "Service: billing-service" in llm.calls[0]["user_message"]


def test_regular_chat_prompt_does_not_enable_service_event_mode(tmp_path: Path) -> None:
    service = ExtractionService(
        llm_service=FakeLLMService('{"memories":[]}'),
        spec_path=_spec(tmp_path),
    )

    prompt = service._build_system_prompt()

    assert "AUTHENTICATED SERVICE EVENT MODE" not in prompt

@pytest.mark.asyncio
async def test_composite_conversation_runs_compositional_prepass(tmp_path: Path) -> None:
    llm = FakeLLMService(
        [
            json.dumps(
                {
                    "entities": [
                        {"name": "analytics platform", "type": "project", "evidence": "message 1"},
                        {"name": "healthcare customers", "type": "company", "evidence": "message 3"},
                    ],
                    "relationships": [
                        {
                            "subject": "User",
                            "relation": "is building",
                            "object": "analytics platform for healthcare customers",
                            "evidence": "combined across turns",
                            "confidence": 0.82,
                        }
                    ],
                }
            ),
            json.dumps(
                {
                    "memories": [
                        {
                            "content": "User is building an analytics platform for healthcare customers",
                            "category": "goal",
                            "importance_score": 7.0,
                            "confidence": 0.86,
                            "reasoning": "The project and audience are stated across multiple turns.",
                        }
                    ],
                    "nothing_to_extract": False,
                }
            ),
        ]
    )
    service = ExtractionService(llm_service=llm, spec_path=_spec(tmp_path))

    result = await service.extract(
        messages=[
            {"role": "user", "content": "We are building an analytics platform, but the product shape is still changing."},
            {"role": "assistant", "content": "What kind of users is it for?"},
            {"role": "user", "content": "Mostly healthcare operations teams who need better weekly reporting."},
            {"role": "assistant", "content": "So healthcare ops is the target?"},
            {"role": "user", "content": "Yes, the goal is to launch a focused healthcare analytics workflow first."},
        ],
        proxy_user_id="proxy-1",
        tenant_id="tenant-1",
        job_id="job-composite-1",
    )

    assert len(llm.calls) == 2
    assert "pass 1" in llm.calls[0]["system_prompt"]
    assert "COMPOSITIONAL EXTRACTION MODE" in llm.calls[1]["system_prompt"]
    assert "Compositional extraction hints" in llm.calls[1]["user_message"]
    assert result.tokens_used == 36
    assert result.memories_extracted == 1
    assert result.extraction_metadata["compositional_pass_attempted"] is True
    assert result.extraction_metadata["compositional_pass_used"] is True
    assert result.extraction_metadata["compositional_relationships"] == 1
    assert result.memories_to_store[0].content == "User is building an analytics platform for healthcare customers"


@pytest.mark.asyncio
async def test_short_conversation_skips_compositional_prepass(tmp_path: Path) -> None:
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
                    }
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
        job_id="job-simple-1",
    )

    assert len(llm.calls) == 1
    assert "pass 1" not in llm.calls[0]["system_prompt"]
    assert result.memories_extracted == 1

@pytest.mark.asyncio
async def test_long_low_signal_conversation_skips_compositional_prepass(tmp_path: Path) -> None:
    llm = FakeLLMService(
        json.dumps(
            {
                "memories": [],
                "nothing_to_extract": True,
                "extraction_notes": "Operational chat only.",
            }
        )
    )
    service = ExtractionService(llm_service=llm, spec_path=_spec(tmp_path))

    result = await service.extract(
        messages=[
            {"role": "user", "content": "Can you explain this warning a bit more? I am reading the logs and trying to understand the output."},
            {"role": "assistant", "content": "Sure, paste the relevant line."},
            {"role": "user", "content": "The output is long and noisy, but I mainly need help understanding the next terminal command."},
            {"role": "assistant", "content": "Let's narrow it down."},
            {"role": "user", "content": "Okay, please keep going with the same debugging flow and don't change anything else."},
        ],
        proxy_user_id="proxy-1",
        tenant_id="tenant-1",
        job_id="job-composite-skip",
    )

    assert len(llm.calls) == 1
    assert result.nothing_to_extract is True
    assert result.extraction_metadata["compositional_pass_attempted"] is False


@pytest.mark.asyncio
async def test_compositional_prepass_failure_falls_back_to_normal_extraction(tmp_path: Path) -> None:
    class FailingThenSuccessLLM(FakeLLMService):
        async def complete(self, **kwargs):
            self.calls.append(kwargs)
            if len(self.calls) == 1:
                raise RuntimeError("prepass unavailable")
            return LLMResponse(
                content=json.dumps(
                    {
                        "memories": [
                            {
                                "content": "User is preparing a healthcare analytics launch",
                                "category": "goal",
                                "importance_score": 7.0,
                                "confidence": 0.82,
                                "reasoning": "The user described the launch goal.",
                            }
                        ],
                        "nothing_to_extract": False,
                    }
                ),
                provider_used="test",
                model_used="fake",
                input_tokens=11,
                output_tokens=7,
                total_tokens=18,
                latency_ms=1,
            )

    llm = FailingThenSuccessLLM("{}")
    service = ExtractionService(llm_service=llm, spec_path=_spec(tmp_path))

    result = await service.extract(
        messages=[
            {"role": "user", "content": "I am building an analytics platform for clinics and hospital operations."},
            {"role": "assistant", "content": "What is the first workflow?"},
            {"role": "user", "content": "The team wants weekly reporting first because customers ask for it constantly."},
            {"role": "assistant", "content": "When do you want to launch?"},
            {"role": "user", "content": "The goal is to launch this healthcare reporting workflow next week."},
        ],
        proxy_user_id="proxy-1",
        tenant_id="tenant-1",
        job_id="job-composite-fail-open",
    )

    assert len(llm.calls) == 2
    assert result.memories_extracted == 1
    assert result.extraction_metadata["compositional_pass_attempted"] is True
    assert result.extraction_metadata["compositional_pass_used"] is False
    assert result.extraction_metadata["compositional_error"] == "RuntimeError"
