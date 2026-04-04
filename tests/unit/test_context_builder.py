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


def test_build_context_bullets_includes_required_metadata() -> None:
    builder = ContextBuilder()
    context = builder.build_context(
        [
            make_memory_result(
                content="User prefers Python for backend work",
                category="preference",
                confidence_score=0.92,
            )
        ]
    )

    assert "User prefers Python for backend work" in context
    assert "category: preference" in context
    assert "confidence: high [0.92]" in context
    assert "learned: 2026-03-24T10:00:00+00:00" in context


def test_build_context_json_returns_memory_array() -> None:
    builder = ContextBuilder()
    context = builder.build_context(
        [make_memory_result(content="User works in healthcare", category="fact")],
        format="json",
    )
    payload = json.loads(context)

    assert isinstance(payload, list)
    assert payload[0]["content"] == "User works in healthcare"
    assert payload[0]["category"] == "fact"
    assert payload[0]["confidence_level"] == "high"
    assert payload[0]["learned_at"] == "2026-03-24T10:00:00+00:00"


def test_build_context_xml_wraps_memories_in_tags() -> None:
    builder = ContextBuilder()
    context = builder.build_context(
        [make_memory_result(content="User is launching soon", category="goal")],
        format="xml",
    )

    assert context.startswith("<memory_context>")
    assert "<category>goal</category>" in context
    assert "<learned_at>2026-03-24T10:00:00+00:00</learned_at>" in context
    assert "</memory_context>" in context


def test_token_budget_drops_lowest_scored_memories_first() -> None:
    builder = ContextBuilder()
    memories = [
        make_memory_result(content="Top memory", final_score=0.95),
        make_memory_result(content="Middle memory", final_score=0.60),
        make_memory_result(content="Lowest memory", final_score=0.20),
    ]

    context = builder.build_context(memories, format="bullets", max_tokens=30)

    assert "Top memory" in context
    assert "Lowest memory" not in context


def test_build_system_prompt_wraps_base_prompt_with_memory_section() -> None:
    builder = ContextBuilder()
    prompt = builder.build_system_prompt(
        "You are a helpful assistant.",
        [make_memory_result(content="User prefers concise responses", category="preference")],
    )

    assert prompt.startswith("You are a helpful assistant.")
    assert "Relevant memory context:" in prompt
    assert "User prefers concise responses" in prompt


def test_build_context_rejects_unknown_format() -> None:
    builder = ContextBuilder()

    with pytest.raises(ValueError):
        builder.build_context([make_memory_result(content="User uses FastAPI")], format="yaml")
