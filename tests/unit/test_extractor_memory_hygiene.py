from __future__ import annotations

from api.schemas.memory_schemas import ExtractedMemory
from api.services.extractor import ExtractionService


def _memory(
    content: str,
    *,
    category: str = "fact",
    importance_score: float = 6.0,
    confidence: float = 0.95,
) -> ExtractedMemory:
    return ExtractedMemory(
        content=content,
        category=category,  # type: ignore[arg-type]
        importance_score=importance_score,
        confidence=confidence,
        expiry="permanent",
        reasoning="Test memory.",
    )


def test_postprocess_drops_vague_project_placeholder_memory() -> None:
    processed = ExtractionService._postprocess_memories(
        [
            _memory(
                "User is building a new project related to images.",
                category="goal",
            )
        ],
        messages=[
            {
                "role": "user",
                "content": "I may work on something with images later.",
            }
        ],
    )

    assert processed == []


def test_postprocess_keeps_concrete_project_memory() -> None:
    processed = ExtractionService._postprocess_memories(
        [
            _memory(
                "User has a project focused on emotion prediction, with the repository located at https://github.com/ADITYA-kus/mlops_mini_pipeline.",
                category="fact",
                importance_score=7.0,
            )
        ],
        messages=[
            {
                "role": "user",
                "content": (
                    "My project was about emotion prediction. "
                    "My repo is https://github.com/ADITYA-kus/mlops_mini_pipeline."
                ),
            }
        ],
    )

    assert [memory.content for memory in processed] == [
        "User has a project focused on emotion prediction, with the repository located at https://github.com/ADITYA-kus/mlops_mini_pipeline."
    ]


def test_postprocess_drops_assistant_advice_memory() -> None:
    processed = ExtractionService._postprocess_memories(
        [
            _memory(
                "User should share their GitHub link.",
                category="goal",
            )
        ],
        messages=[
            {
                "role": "user",
                "content": "I built an emotion prediction pipeline with Docker and AWS.",
            }
        ],
    )

    assert processed == []


def test_postprocess_keeps_user_stated_goal_with_evidence() -> None:
    processed = ExtractionService._postprocess_memories(
        [
            _memory(
                "User wants to become a Data Scientist at Google within the next 3 years.",
                category="goal",
                importance_score=9.0,
            )
        ],
        messages=[
            {
                "role": "user",
                "content": "I want to become a data scientist at Google in the next 3 years.",
            }
        ],
    )

    assert [memory.content for memory in processed] == [
        "User wants to become a Data Scientist at Google within the next 3 years."
    ]
