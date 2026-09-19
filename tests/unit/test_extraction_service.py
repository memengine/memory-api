from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from api.schemas.extraction_schemas import PendingExtractedMemory
from api.services.extraction_service import ExtractionError, ExtractionService
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
        messages=[
            {
                "role": "user",
                "content": "Remember I prefer concise Python-first explanations.",
            }
        ],
        proxy_user_id="proxy-1",
        tenant_id="tenant-1",
        job_id="job-1",
    )

    assert result.memories_extracted == 1
    assert result.memories_filtered == 2
    assert result.tokens_used == 18
    assert result.provider_used == "test"
    assert (
        result.memories_to_store[0].content
        == "User prefers concise Python-first explanations"
    )
    assert llm.calls[0]["response_format"] == "json"
    assert "What Should NEVER" not in llm.calls[0]["system_prompt"]


@pytest.mark.asyncio
async def test_extract_keeps_declarative_preference_that_starts_with_when(
    tmp_path: Path,
) -> None:
    service = ExtractionService(
        llm_service=FakeLLMService(
            json.dumps(
                {
                    "memories": [
                        {
                            "content": "User prefers concise Python-first coding examples.",
                            "category": "preference",
                            "importance_score": 7.0,
                            "confidence": 0.92,
                            "evidence_turns": [0],
                            "evidence_relation": "direct_user_statement",
                            "reasoning": "The user directly stated a durable format preference.",
                        }
                    ],
                    "nothing_to_extract": False,
                }
            )
        ),
        spec_path=_spec(tmp_path),
    )

    result = await service.extract(
        messages=[
            {
                "role": "user",
                "content": "When you explain coding topics to me, I prefer concise Python-first examples.",
                "turn_id": "external:turn-100",
                "external_turn_id": "turn-100",
                "turn_content_sha256": "a" * 64,
            }
        ],
        proxy_user_id="proxy-1",
        tenant_id="tenant-1",
        job_id="job-1",
    )

    assert result.memories_extracted == 1
    assert result.memories_to_store[0].category == "preference"
    evidence = result.memories_to_store[0].validated_evidence
    assert evidence["citation_mode"] == "model_cited"
    assert evidence["turn_indexes"] == [0]
    assert evidence["user_turn_indexes"] == [0]
    assert evidence["relation"] == "direct_user_statement"
    assert evidence["turn_references"] == [
        {
            "turn_index": 0,
            "turn_id": "external:turn-100",
            "external_turn_id": "turn-100",
            "content_sha256": "a" * 64,
            "role": "user",
            "source_kind": "",
        }
    ]
    assert evidence["proposal_turn_id"] is None
    assert evidence["authority"] == {"level": 20, "label": "client_assertion"}
    assert evidence["validation"] == {
        "accepted": True,
        "reason": "direct_user_statement",
    }
    assert evidence["extraction"]["provider"] == "test"
    assert evidence["extraction"]["model"] == "fake"
    assert (
        result.extraction_metadata["prompt_context"]["existing_memory_context_tokens"]
        == 0
    )
    assert result.extraction_metadata["primary_pass"]["total_tokens"] == 18
    assert (
        result.extraction_metadata["compositional_pass_metrics"]["attempted"] is False
    )


def test_existing_memory_context_is_token_bounded(tmp_path: Path) -> None:
    service = ExtractionService(
        llm_service=FakeLLMService('{"memories":[]}'), spec_path=_spec(tmp_path)
    )
    memories = [
        SimpleNamespace(
            content="detail " * 80, category="fact", importance_score=20 - index
        )
        for index in range(20)
    ]

    rendered = service._append_existing_memory_context(
        "[turn 0][user]: hello", memories
    )
    metrics = service._prompt_context_metrics(
        conversation="[turn 0][user]: hello",
        before_existing_context="[turn 0][user]: hello",
        after_existing_context=rendered,
        existing_memories=memories,
    )

    memory_lines = [line for line in rendered.splitlines() if line.startswith("- [")]
    assert service._count_tokens("\n".join(memory_lines)) <= 1200
    assert 0 < len(memory_lines) < len(memories)
    assert metrics["existing_memories_included"] == len(memory_lines)
    assert metrics["existing_memory_context_budget_tokens"] == 1200


def test_single_oversized_turn_is_hard_token_bounded(tmp_path: Path) -> None:
    service = ExtractionService(
        llm_service=FakeLLMService('{"memories":[]}'), spec_path=_spec(tmp_path)
    )

    conversation = service._build_conversation_string(
        [{"role": "user", "content": "durable detail " * 6_000, "_turn_index": 0}]
    )

    assert conversation.startswith("[turn 0][user]:")
    assert service._count_tokens(conversation) <= 5_000


@pytest.mark.asyncio
async def test_primary_input_has_strict_total_budget_and_truthful_visible_turns(
    tmp_path: Path,
) -> None:
    llm = FakeLLMService('{"memories":[],"nothing_to_extract":true}')
    service = ExtractionService(llm_service=llm, spec_path=_spec(tmp_path))
    oversized = "durable detail " * 6_000

    result = await service.extract(
        messages=[
            {"role": "user", "source_kind": "direct_user_input", "content": oversized},
            {
                "role": "user",
                "source_kind": "direct_user_input",
                "content": "I prefer Python examples.",
            },
        ],
        proxy_user_id="proxy-budget",
        tenant_id="tenant-budget",
        job_id="job-budget",
        existing_memories=[
            SimpleNamespace(
                content="context " * 500, category="fact", importance_score=10
            )
        ],
    )

    primary_call = llm.calls[-1]
    assert (
        service._count_tokens(str(primary_call["system_prompt"]))
        + service._count_tokens(str(primary_call["user_message"]))
        <= 10_000
    )
    assert "[source direct_user_input]" in str(primary_call["user_message"])
    assert (
        result.extraction_metadata["prompt_context"]["primary_input_tokens"] <= 10_000
    )
    assert result.extraction_metadata["prompt_context"]["visible_turn_count"] == 1


def test_composition_hints_are_token_bounded(tmp_path: Path) -> None:
    service = ExtractionService(
        llm_service=FakeLLMService('{"memories":[]}'), spec_path=_spec(tmp_path)
    )
    signals = {
        "entities": [
            {
                "name": f"project-{index}",
                "type": "project",
                "evidence": "evidence " * 200,
            }
            for index in range(12)
        ],
        "relationships": [],
    }

    rendered = service._append_composition_context("[turn 0][user]: hello", signals)
    hint_text = rendered.split("\n\n", 1)[1]

    assert service._count_tokens(hint_text) <= 800
    assert "[turn 0][user]: hello" in rendered


def test_question_only_guard_still_rejects_unpunctuated_question() -> None:
    assert ExtractionService._is_question_only("When should I deploy the API") is True




def test_question_only_guard_keeps_declaration_after_question() -> None:
    assert (
        ExtractionService._is_question_only(
            "Can you recommend a key label? I now keep the spare key in the locked cabinet."
        )
        is False
    )


def test_question_only_guard_keeps_declaration_before_question() -> None:
    assert (
        ExtractionService._is_question_only(
            "I prefer concise numbered steps. Can you remember that?"
        )
        is False
    )


def test_question_only_guard_rejects_multiple_question_clauses() -> None:
    assert (
        ExtractionService._is_question_only(
            "Can you recommend a key label? Would a colored tag help?"
        )
        is True
    )


@pytest.mark.asyncio
async def test_extract_keeps_memory_from_mixed_question_and_declaration(
    tmp_path: Path,
) -> None:
    service = ExtractionService(
        llm_service=FakeLLMService(
            json.dumps(
                {
                    "memories": [
                        {
                            "content": "User now keeps the spare office key in the locked cabinet by their desk.",
                            "category": "fact",
                            "importance_score": 5.0,
                            "confidence": 0.8,
                            "evidence_turns": [0],
                            "evidence_relation": "direct_user_statement",
                            "reasoning": "The user directly corrected the key location.",
                        }
                    ],
                    "nothing_to_extract": False,
                }
            )
        ),
        spec_path=_spec(tmp_path),
    )

    result = await service.extract(
        messages=[
            {
                "role": "user",
                "content": "Can you recommend a key label? I now keep the spare office key in the locked cabinet by my desk.",
            }
        ],
        proxy_user_id="proxy-mixed-turn",
        tenant_id="tenant-1",
        job_id="job-mixed-turn",
    )

    assert result.memories_extracted == 1
    assert result.memories_filtered == 0
    assert result.extraction_metadata["candidate_validation"]["rejection_counts"] == {}


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
        messages=[
            {"role": "user", "content": "Maybe keep replies short for hard topics."}
        ],
        proxy_user_id="proxy-1",
        tenant_id="tenant-1",
        job_id="job-pending-1",
    )

    assert result.memories_extracted == 0
    assert result.pending_candidates_count == 1
    assert result.memories_filtered == 1
    assert (
        result.pending_candidates[0].content
        == "User may prefer short replies for difficult topics"
    )
    assert result.pending_candidates[0].confidence == 0.58


@pytest.mark.asyncio
async def test_importance_one_is_valid_under_extraction_contract(
    tmp_path: Path,
) -> None:
    service = ExtractionService(
        llm_service=FakeLLMService(
            json.dumps(
                {
                    "memories": [
                        {
                            "content": "User prefers a dark theme for this client project only.",
                            "category": "preference",
                            "importance_score": 1.0,
                            "confidence": 0.9,
                            "evidence_turns": [0],
                            "evidence_relation": "direct_user_statement",
                            "reasoning": "The preference is explicit but narrowly scoped.",
                        }
                    ],
                    "nothing_to_extract": False,
                }
            )
        ),
        spec_path=_spec(tmp_path),
    )

    result = await service.extract(
        messages=[
            {
                "role": "user",
                "content": "For this client project only, use a dark theme.",
            }
        ],
        proxy_user_id="proxy-low-importance",
        tenant_id="tenant-1",
        job_id="job-low-importance",
    )

    assert result.memories_extracted == 1
    assert result.memories_to_store[0].importance_score == 1.0
    assert result.memories_filtered == 0


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
            {
                "role": "user",
                "content": "Can you explain this warning a bit more? I am reading logs.",
            },
            {"role": "assistant", "content": "Sure, paste the relevant line."},
            {
                "role": "user",
                "content": "Okay, please keep going with the same debugging flow and don't change anything else.",
            },
        ],
        proxy_user_id="proxy-1",
        tenant_id="tenant-1",
        job_id="job-temp-debug-filter",
    )

    assert result.memories_extracted == 0
    assert result.pending_candidates_count == 0
    assert result.memories_filtered == 1
    assert result.extraction_metadata["candidate_validation"]["rejection_counts"] == {
        "temporary_session_directive": 1
    }


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
    assert "independently correctable and independently reusable claim" in prompt
    assert "cannot enforce an explicit end date" in prompt
    assert "Do not default every plausible memory" in prompt
    assert "Do not default every memory to 5" in prompt
    assert "is a pending goal, not a current fact" in prompt
    assert "after an unrelated question or request" in prompt


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
            {
                "role": "user",
                "content": "What subscription plan is this customer using?",
            },
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

@pytest.mark.asyncio
async def test_regular_chat_rejects_memory_supported_only_by_assistant(
    tmp_path: Path,
) -> None:
    llm = FakeLLMService(
        json.dumps(
            {
                "memories": [
                    {
                        "content": "User prefers clear technical explanations without jargon",
                        "category": "preference",
                        "importance_score": 7.0,
                        "confidence": 0.91,
                        "evidence_turns": [0, 1],
                        "reasoning": "The assistant offered this response style.",
                    }
                ],
                "nothing_to_extract": False,
            }
        )
    )
    service = ExtractionService(llm_service=llm, spec_path=_spec(tmp_path))

    result = await service.extract(
        messages=[
            {
                "role": "user",
                "content": "How should you explain technical problems to me?",
            },
            {
                "role": "assistant",
                "content": "I will explain them clearly and avoid jargon.",
            },
        ],
        proxy_user_id="proxy-1",
        tenant_id="tenant-1",
        job_id="job-assistant-only",
    )

    assert result.memories_extracted == 0
    assert result.pending_candidates_count == 0
    assert result.memories_filtered == 1


@pytest.mark.asyncio
async def test_regular_chat_keeps_explicit_user_preference(tmp_path: Path) -> None:
    llm = FakeLLMService(
        json.dumps(
            {
                "memories": [
                    {
                        "content": "User prefers concise step-by-step technical explanations",
                        "category": "preference",
                        "importance_score": 8.0,
                        "confidence": 0.94,
                        "evidence_turns": [0],
                        "reasoning": "The user explicitly stated this preference.",
                    }
                ],
                "nothing_to_extract": False,
            }
        )
    )
    service = ExtractionService(llm_service=llm, spec_path=_spec(tmp_path))

    result = await service.extract(
        messages=[
            {
                "role": "user",
                "content": "I prefer concise, step-by-step technical explanations.",
            },
        ],
        proxy_user_id="proxy-1",
        tenant_id="tenant-1",
        job_id="job-user-preference",
    )

    assert result.memories_extracted == 1


@pytest.mark.asyncio
async def test_regular_chat_does_not_promote_assistant_confirmation_before_phase3a(
    tmp_path: Path,
) -> None:
    llm = FakeLLMService(
        json.dumps(
            {
                "memories": [
                    {
                        "content": "User prefers concise step-by-step explanations",
                        "category": "preference",
                        "importance_score": 8.0,
                        "confidence": 0.9,
                        "evidence_turns": [1, 2],
                        "evidence_relation": "user_confirmed_assistant_proposal",
                        "proposal_turn": 1,
                        "reasoning": "The user confirmed the assistant's proposed style.",
                    }
                ],
                "nothing_to_extract": False,
            }
        )
    )
    service = ExtractionService(llm_service=llm, spec_path=_spec(tmp_path))
    result = await service.extract(
        messages=[
            {"role": "user", "content": "Help me choose a response style."},
            {
                "role": "assistant",
                "content": "Would you prefer concise step-by-step explanations?",
                "source_kind": "assistant_output",
                "is_memory_proposal": True,
            },
            {"role": "user", "content": "Exactly. Please remember that."},
        ],
        proxy_user_id="proxy-1",
        tenant_id="tenant-1",
        job_id="job-user-confirmation",
    )

    assert result.memories_extracted == 0
    assert result.memories_filtered == 1



@pytest.mark.asyncio
async def test_phase3a_accepts_natural_confirmation_only_for_active_single_proposal(
    tmp_path: Path,
) -> None:
    llm = FakeLLMService(
        json.dumps(
            {
                "memories": [
                    {
                        "content": "User prefers concise step-by-step explanations",
                        "category": "preference",
                        "importance_score": 8.0,
                        "confidence": 0.9,
                        "evidence_turns": [1, 2],
                        "evidence_relation": "user_confirmed_assistant_proposal",
                        "proposal_turn": 1,
                        "reasoning": "The user naturally accepted the registered proposal.",
                    }
                ],
                "nothing_to_extract": False,
            }
        )
    )
    service = ExtractionService(
        llm_service=llm,
        spec_path=_spec(tmp_path),
        proposal_confirmation_enabled=True,
    )
    messages = [
        {
            "role": "user",
            "content": "Help me choose a response style.",
            "turn_id": "turn-0",
            "turn_content_sha256": "hash-0",
        },
        {
            "role": "assistant",
            "content": "I can remember that you prefer concise step-by-step explanations.",
            "source_kind": "assistant_output",
            "is_memory_proposal": True,
            "turn_id": "proposal-turn-1",
            "turn_content_sha256": "hash-1",
        },
        {
            "role": "user",
            "content": "That captures it perfectly. Keep it.",
            "source_kind": "direct_user_input",
            "turn_id": "turn-2",
            "turn_content_sha256": "hash-2",
        },
    ]

    result = await service.extract(
        messages=messages,
        proxy_user_id="proxy-1",
        tenant_id="tenant-1",
        job_id="job-user-confirmation",
        proposal_context=[
            {
                "id": "proposal-id-1",
                "group_id": "group-1",
                "ordinal": 1,
                "turn_index": 1,
                "turn_id": "proposal-turn-1",
                "content_sha256": "hash-1",
            }
        ],
    )

    assert result.memories_extracted == 1
    evidence = result.memories_to_store[0].validated_evidence
    assert evidence["relation"] == "user_confirmed_assistant_proposal"
    assert evidence["proposal"] == {
        "id": "proposal-id-1",
        "group_id": "group-1",
        "ordinal": 1,
    }
    assert result.extraction_metadata["proposal_confirmation"] == {
        "enabled": True,
        "active_proposal_count": 1,
        "accepted": 1,
        "pending": 0,
        "rejected_reasons": {},
    }


@pytest.mark.asyncio
async def test_phase3a_routes_ambiguous_multi_proposal_confirmation_to_pending(
    tmp_path: Path,
) -> None:
    llm = FakeLLMService(
        json.dumps(
            {
                "memories": [
                    {
                        "content": "User prefers numbered troubleshooting steps",
                        "category": "preference",
                        "importance_score": 8.0,
                        "confidence": 0.9,
                        "evidence_turns": [2, 3],
                        "evidence_relation": "user_confirmed_assistant_proposal",
                        "proposal_turn": 2,
                        "reasoning": "The user accepted a proposal without identifying which one.",
                    }
                ],
                "nothing_to_extract": False,
            }
        )
    )
    service = ExtractionService(
        llm_service=llm,
        spec_path=_spec(tmp_path),
        proposal_confirmation_enabled=True,
    )
    messages = [
        {
            "role": "assistant",
            "content": "I can remember that you prefer concise answers.",
            "source_kind": "assistant_output",
            "is_memory_proposal": True,
            "turn_id": "proposal-turn-1",
            "turn_content_sha256": "hash-1",
        },
        {
            "role": "user",
            "content": "What else?",
            "source_kind": "direct_user_input",
            "turn_id": "turn-1",
            "turn_content_sha256": "hash-u1",
        },
        {
            "role": "assistant",
            "content": "I can remember that you prefer numbered troubleshooting steps.",
            "source_kind": "assistant_output",
            "is_memory_proposal": True,
            "turn_id": "proposal-turn-2",
            "turn_content_sha256": "hash-2",
        },
        {
            "role": "user",
            "content": "Yes, keep that.",
            "source_kind": "direct_user_input",
            "turn_id": "turn-3",
            "turn_content_sha256": "hash-u3",
        },
    ]
    active = [
        {
            "id": "proposal-id-1",
            "group_id": "group-1",
            "ordinal": 1,
            "turn_index": 0,
            "turn_id": "proposal-turn-1",
            "content_sha256": "hash-1",
        },
        {
            "id": "proposal-id-2",
            "group_id": "group-1",
            "ordinal": 2,
            "turn_index": 2,
            "turn_id": "proposal-turn-2",
            "content_sha256": "hash-2",
        },
    ]

    result = await service.extract(
        messages=messages,
        proxy_user_id="proxy-1",
        tenant_id="tenant-1",
        job_id="job-ambiguous-confirmation",
        proposal_context=active,
    )

    assert result.memories_extracted == 0
    assert result.pending_candidates_count == 1
    assert result.pending_candidates[0].candidate_reason == "ambiguous_proposal_reference"
    assert result.extraction_metadata["candidate_validation"]["rejection_counts"] == {
        "ambiguous_proposal_reference": 1
    }
    assert result.extraction_metadata["proposal_confirmation"] == {
        "enabled": True,
        "active_proposal_count": 2,
        "accepted": 0,
        "pending": 1,
        "rejected_reasons": {},
    }

def test_ineligible_tool_text_cannot_support_direct_user_memory(tmp_path: Path) -> None:
    candidate = PendingExtractedMemory(
        content="User prefers Python examples",
        category="preference",
        importance_score=7,
        confidence=0.9,
        reasoning="synthetic policy test",
    )
    evidence = ExtractionService._validated_user_evidence(
        candidate,
        [
            {
                "role": "user",
                "source_kind": "direct_user_input",
                "content": "Hello there.",
            },
            {
                "role": "user",
                "source_kind": "tool_output",
                "content": "User prefers Python examples.",
            },
        ],
        [0, 1],
        "direct_user_statement",
    )
    assert evidence == {}


def test_model_cannot_promote_rejected_document_proposal(tmp_path: Path) -> None:
    candidate = PendingExtractedMemory(
        content="User prefers Python examples",
        category="preference",
        importance_score=7,
        confidence=0.9,
        reasoning="synthetic policy test",
    )
    evidence = ExtractionService._validated_user_evidence(
        candidate,
        [
            {
                "role": "assistant",
                "source_kind": "fetched_document",
                "content": "User prefers Python examples.",
            },
            {
                "role": "user",
                "source_kind": "direct_user_input",
                "content": "No, that is wrong.",
            },
        ],
        [0, 1],
        "user_confirmed_assistant_proposal",
        0,
    )
    assert evidence == {}


def test_evidence_outside_visible_prompt_is_rejected(tmp_path: Path) -> None:
    candidate = PendingExtractedMemory(
        content="User prefers Python examples",
        category="preference",
        importance_score=7,
        confidence=0.9,
        reasoning="synthetic policy test",
    )
    assert (
        ExtractionService._validated_user_evidence(
            candidate,
            [{"role": "user", "content": "I prefer Python examples."}],
            [0],
            "direct_user_statement",
            visible_turn_indexes=set(),
        )
        == {}
    )


def test_regular_chat_prompt_does_not_enable_service_event_mode(tmp_path: Path) -> None:
    service = ExtractionService(
        llm_service=FakeLLMService('{"memories":[]}'),
        spec_path=_spec(tmp_path),
    )

    prompt = service._build_system_prompt()

    assert "AUTHENTICATED SERVICE EVENT MODE" not in prompt


@pytest.mark.asyncio
async def test_composite_conversation_runs_compositional_prepass(
    tmp_path: Path,
) -> None:
    llm = FakeLLMService(
        [
            json.dumps(
                {
                    "entities": [
                        {
                            "name": "analytics platform",
                            "type": "project",
                            "evidence": "message 1",
                        },
                        {
                            "name": "healthcare customers",
                            "type": "company",
                            "evidence": "message 3",
                        },
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
            {
                "role": "user",
                "content": "We are building an analytics platform, but the product shape is still changing.",
            },
            {"role": "assistant", "content": "What kind of users is it for?"},
            {
                "role": "user",
                "content": "Mostly healthcare operations teams who need better weekly reporting.",
            },
            {"role": "assistant", "content": "So healthcare ops is the target?"},
            {
                "role": "user",
                "content": "Yes, the goal is to launch a focused healthcare analytics workflow first.",
            },
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
    assert (
        result.memories_to_store[0].content
        == "User is building an analytics platform for healthcare customers"
    )


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
        messages=[
            {
                "role": "user",
                "content": "Remember I prefer concise Python-first explanations.",
            }
        ],
        proxy_user_id="proxy-1",
        tenant_id="tenant-1",
        job_id="job-simple-1",
    )

    assert len(llm.calls) == 1
    assert "pass 1" not in llm.calls[0]["system_prompt"]
    assert result.memories_extracted == 1


@pytest.mark.asyncio
async def test_long_low_signal_conversation_skips_compositional_prepass(
    tmp_path: Path,
) -> None:
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
            {
                "role": "user",
                "content": "Can you explain this warning a bit more? I am reading the logs and trying to understand the output.",
            },
            {"role": "assistant", "content": "Sure, paste the relevant line."},
            {
                "role": "user",
                "content": "The output is long and noisy, but I mainly need help understanding the next terminal command.",
            },
            {"role": "assistant", "content": "Let's narrow it down."},
            {
                "role": "user",
                "content": "Okay, please keep going with the same debugging flow and don't change anything else.",
            },
        ],
        proxy_user_id="proxy-1",
        tenant_id="tenant-1",
        job_id="job-composite-skip",
    )

    assert len(llm.calls) == 1
    assert result.nothing_to_extract is True
    assert result.extraction_metadata["compositional_pass_attempted"] is False


@pytest.mark.asyncio
async def test_compositional_prepass_failure_falls_back_to_normal_extraction(
    tmp_path: Path,
) -> None:
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
            {
                "role": "user",
                "content": "I am building an analytics platform for clinics and hospital operations.",
            },
            {"role": "assistant", "content": "What is the first workflow?"},
            {
                "role": "user",
                "content": "The team wants weekly reporting first because customers ask for it constantly.",
            },
            {"role": "assistant", "content": "When do you want to launch?"},
            {
                "role": "user",
                "content": "The goal is to launch this healthcare reporting workflow next week.",
            },
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
