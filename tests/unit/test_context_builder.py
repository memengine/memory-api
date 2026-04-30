from __future__ import annotations

import json

import pytest

from api.services.context_builder import ContextBuilder
from api.services.retriever import MemoryResult


def make_memory_result(
    *,
    content: str,
    category: str = "fact",
    confidence_score: float = 0.9,
    importance_score: float = 7.0,
    final_score: float = 0.8,
    created_at: str | None = "2026-03-24T10:00:00+00:00",
) -> MemoryResult:
    return MemoryResult(
        id=content.lower().replace(" ", "-"),
        content=content,
        category=category,
        importance_score=importance_score,
        confidence_score=confidence_score,
        semantic_score=0.75,
        recency_score=0.5,
        final_score=final_score,
        agent_id=None,
        previous_version_id=None,
        last_accessed_at="2026-03-24T11:00:00+00:00",
        created_at=created_at,
    )


def test_build_bullets_groups_by_category_without_internal_metrics() -> None:
    builder = ContextBuilder()
    result = builder.build(
        [
            make_memory_result(
                content="Prefers concise technical explanations over theory",
                category="preference",
                confidence_score=0.92,
            ),
            make_memory_result(
                content="Has 3 years of FastAPI experience",
                category="expertise",
            ),
        ]
    )

    assert result.system_prompt_addition.startswith("What you know about this user:")
    assert "Skills & expertise:" in result.system_prompt_addition
    assert "Preferences:" in result.system_prompt_addition
    assert "- Has 3 years of FastAPI experience" in result.system_prompt_addition
    assert "- Prefers concise technical explanations over theory" in result.system_prompt_addition
    assert "confidence" not in result.system_prompt_addition
    assert "importance" not in result.system_prompt_addition
    assert result.memory_count == 2
    assert result.token_count > 0


def test_build_json_returns_grouped_memory_object_without_scores() -> None:
    builder = ContextBuilder()
    result = builder.build(
        [make_memory_result(content="User works in healthcare", category="fact")],
        format="json",
    )
    payload = json.loads(result.system_prompt_addition)

    assert payload == {"memories": {"fact": ["User works in healthcare"]}}
    assert "importance_score" not in result.system_prompt_addition
    assert "confidence_score" not in result.system_prompt_addition


def test_build_xml_uses_memory_category_attributes() -> None:
    builder = ContextBuilder()
    result = builder.build(
        [make_memory_result(content="User is launching soon", category="goal")],
        format="xml",
    )

    assert result.system_prompt_addition.startswith("<memory_context>")
    assert '<memory category="goal">User is launching soon</memory>' in result.system_prompt_addition
    assert "</memory_context>" in result.system_prompt_addition


def test_filters_low_importance_memories() -> None:
    builder = ContextBuilder()
    result = builder.build(
        [
            make_memory_result(content="Low value note", importance_score=2.0, final_score=0.99),
            make_memory_result(content="Important note", importance_score=6.0, final_score=0.7),
        ],
    )

    assert "Important note" in result.system_prompt_addition
    assert "Low value note" not in result.system_prompt_addition
    assert result.memory_count == 1


def test_deduplicates_near_duplicate_memories_at_build_time() -> None:
    builder = ContextBuilder()
    result = builder.build(
        [
            make_memory_result(content="User prefers Python examples", final_score=0.95),
            make_memory_result(content="User prefers Python examples.", final_score=0.90),
        ],
    )

    assert result.system_prompt_addition.count("User prefers Python examples") == 1
    assert result.memory_count == 1


def test_token_budget_drops_lowest_importance_memories_after_top_three() -> None:
    builder = ContextBuilder()
    memories = [
        make_memory_result(content="Top memory one " * 10, importance_score=9.0, final_score=0.99),
        make_memory_result(content="Top memory two " * 10, importance_score=8.0, final_score=0.98),
        make_memory_result(content="Top memory three " * 10, importance_score=7.0, final_score=0.97),
        make_memory_result(content="Lowest importance extra memory " * 10, importance_score=3.1, final_score=0.96),
        make_memory_result(content="Higher importance extra memory " * 10, importance_score=6.0, final_score=0.95),
    ]

    result = builder.build(memories, format="bullets", max_tokens=80)

    assert "Top memory one" in result.system_prompt_addition
    assert "Top memory two" in result.system_prompt_addition
    assert "Top memory three" in result.system_prompt_addition
    assert "Lowest importance extra memory" not in result.system_prompt_addition
    assert result.memories_dropped >= 1


def test_truncates_very_long_memory_content() -> None:
    builder = ContextBuilder()
    long_content = "User prefers " + ("very detailed " * 30) + "answers"

    result = builder.build([make_memory_result(content=long_content, category="preference")])

    assert "..." in result.system_prompt_addition
    assert len(result.system_prompt_addition) < len(long_content) + 80


def test_build_system_prompt_wraps_base_prompt_with_memory_section() -> None:
    builder = ContextBuilder()
    prompt = builder.build_system_prompt(
        "You are a helpful assistant.",
        [make_memory_result(content="User prefers concise responses", category="preference")],
    )

    assert prompt.startswith("You are a helpful assistant.")
    assert "Relevant memory context:" in prompt
    assert "What you know about this user:" in prompt
    assert "User prefers concise responses" in prompt


def test_build_rejects_unknown_format() -> None:
    builder = ContextBuilder()

    with pytest.raises(ValueError):
        builder.build([make_memory_result(content="User uses FastAPI")], format="yaml")
