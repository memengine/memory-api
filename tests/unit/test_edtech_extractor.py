from __future__ import annotations

import uuid

import pytest

from api.db.models import EdTechMemory
from api.services.edtech.edtech_extractor import EdTechExtractionError
from api.services.edtech.edtech_extractor import EdTechExtractor
from api.services.edtech.prompt_builder import EdTechPromptBuilder


class DummySession:
    def add(self, item):
        return None


def test_prompt_builder_compresses_existing_memory() -> None:
    memory = EdTechMemory(
        id=uuid.uuid4(),
        proxy_user_id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        grade_level="Class 10",
        board_or_curriculum="CBSE",
        weak_topics=[{"topic": "quadratic equations", "severity": "moderate"}],
        strong_topics=[{"topic": "linear equations", "confidence": 0.9}],
        language_profile={"primary": "Hindi", "explanation_preference": "Hinglish"},
    )

    compressed = EdTechPromptBuilder().compress_existing_memory(memory)

    assert compressed is not None
    assert "Class 10" in compressed
    assert "quadratic equations" in compressed
    assert "Hinglish" in compressed


def test_extractor_merges_topics_without_replacing_existing_profile() -> None:
    extractor = EdTechExtractor.__new__(EdTechExtractor)
    extractor.session = DummySession()
    memory = EdTechMemory(
        id=uuid.uuid4(),
        proxy_user_id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        weak_topics=[{"topic": "fractions", "severity": "mild", "attempts": 1}],
        strong_topics=[{"topic": "linear equations", "confidence": 0.8}],
        explanation_style={"primary": "worked_examples"},
    )
    updated: set[str] = set()

    extractor._merge_extracted(
        memory,
        {
            "weak_topics": [{"topic": "fractions", "severity": "severe", "attempts": 3}],
            "strong_topics": [{"topic": "trigonometry", "confidence": 0.7}],
            "explanation_style": {"needs_step_by_step": True},
            "last_topic_studied": "fractions",
        },
        updated,
    )
    extractor._update_forgetting_stages(memory, {"weak_topics": [{"topic": "fractions"}]})

    assert {"weak_topics", "strong_topics", "explanation_style", "last_topic_studied"} <= updated
    assert memory.weak_topics[0]["severity"] == "severe"
    assert any(topic["topic"] == "trigonometry" for topic in memory.strong_topics)
    assert memory.explanation_style == {"primary": "worked_examples", "needs_step_by_step": True}
    assert memory.forgetting_stages["fractions"]["stage"] == "fresh"


def test_invalid_llm_json_raises_extraction_error() -> None:
    extractor = EdTechExtractor.__new__(EdTechExtractor)
    with pytest.raises(EdTechExtractionError):
        extractor._parse_response("not-json")


def test_fallback_extracts_obvious_student_profile_signals() -> None:
    extractor = EdTechExtractor.__new__(EdTechExtractor)

    extracted = extractor._fallback_extract_from_user_text(
        [
            {
                "role": "user",
                "content": "I am studying in class 12 and focusing on boards and CUET.",
            },
            {
                "role": "user",
                "content": "My parents ask for JEE Main side by side. I am stuck in chemistry because it feels unformulated.",
            },
            {
                "role": "user",
                "content": "In math ellipse, chemistry chemical bonding, physics kinematics.",
            },
        ]
    )

    assert extracted["grade_level"] == "Class 12"
    assert "exam_name" not in extracted
    assert any(topic["topic"] == "Chemistry" for topic in extracted["weak_topics"])
    last_topics = extracted["last_topic_studied"]
    assert "Math Ellipse" in last_topics
    assert "Chemistry Chemical Bonding" in last_topics
    assert "Physics Kinematics" in last_topics


def test_fallback_does_not_infer_exam_date_from_loose_timeline() -> None:
    extractor = EdTechExtractor.__new__(EdTechExtractor)

    extracted = extractor._fallback_extract_from_user_text(
        [
            {
                "role": "user",
                "content": "I am preparing for JEE Main side by side with boards.",
            }
        ]
    )

    assert "exam_date" not in extracted


def test_fallback_extracts_exam_name_generically_from_user_words() -> None:
    extractor = EdTechExtractor.__new__(EdTechExtractor)

    extracted = extractor._fallback_extract_from_user_text(
        [
            {
                "role": "user",
                "content": "I am preparing for the National Design Entrance exam and also focusing on my school final exam.",
            }
        ]
    )

    assert "National Design Entrance" in extracted["exam_name"]
    assert "School Final" in extracted["exam_name"]


def test_fallback_rejects_conversational_fragments_as_exam_or_topic() -> None:
    extractor = EdTechExtractor.__new__(EdTechExtractor)

    extracted = extractor._fallback_extract_from_user_text(
        [
            {
                "role": "user",
                "content": "the big problem is about organic chemistry then what can I do",
            },
            {
                "role": "user",
                "content": "everything",
            },
        ]
    )

    assert "exam_name" not in extracted
    assert "last_topic_studied" not in extracted


def test_fallback_does_not_treat_board_word_as_curriculum_without_explicit_board_statement() -> None:
    extractor = EdTechExtractor.__new__(EdTechExtractor)

    extracted = extractor._fallback_extract_from_user_text(
        [
            {
                "role": "user",
                "content": "What are the most important topics? Our board is also in next 10 days. I know GOC.",
            }
        ]
    )

    assert "board_or_curriculum" not in extracted


def test_fallback_extracts_difficulty_phrase_as_weak_topic() -> None:
    extractor = EdTechExtractor.__new__(EdTechExtractor)

    extracted = extractor._fallback_extract_from_user_text(
        [
            {
                "role": "user",
                "content": "I know GOC but difficulty in Chemical Tests for Distinction.",
            }
        ]
    )

    assert any(topic["topic"] == "Chemical Tests for Distinction" for topic in extracted["weak_topics"])


def test_fallback_strips_dates_from_exam_names() -> None:
    extractor = EdTechExtractor.__new__(EdTechExtractor)

    extracted = extractor._fallback_extract_from_user_text(
        [
            {
                "role": "user",
                "content": (
                    "My design entrance exam is on 10 July. "
                    "Our board exam is in 10 days."
                ),
            }
        ]
    )

    assert "Design Entrance" in extracted["exam_name"]
    assert "Board" in extracted["exam_name"]
    assert "10" not in extracted["exam_name"]
    assert "July" not in extracted["exam_name"]
    assert "Days" not in extracted["exam_name"]


def test_fallback_rejects_vague_short_answer_as_last_topic() -> None:
    extractor = EdTechExtractor.__new__(EdTechExtractor)

    extracted = extractor._fallback_extract_from_user_text(
        [
            {"role": "assistant", "content": "Which topic should we continue?"},
            {"role": "user", "content": "not remember"},
        ]
    )

    assert "last_topic_studied" not in extracted


def test_fallback_extracts_higher_ed_profile_and_exam_context() -> None:
    extractor = EdTechExtractor.__new__(EdTechExtractor)

    extracted = extractor._fallback_extract_from_user_text(
        [
            {
                "role": "user",
                "content": (
                    "I am a 2nd year ECE student. "
                    "My semester exam is on 10 July, and I am preparing for GATE exam."
                ),
            }
        ]
    )

    assert extracted["grade_level"] == "2nd Year ECE"
    assert "Semester" in extracted["exam_name"]
    assert "Gate" in extracted["exam_name"]
    assert "10" not in extracted["exam_name"]
    assert "July" not in extracted["exam_name"]


def test_fallback_exam_name_does_not_cross_sentence_boundaries() -> None:
    extractor = EdTechExtractor.__new__(EdTechExtractor)

    extracted = extractor._fallback_extract_from_user_text(
        [
            {
                "role": "user",
                "content": "My portfolio is on github.com/example/project. My semester exam is on 10 July.",
            },
            {
                "role": "user",
                "content": "I am preparing for GATE exam.",
            },
        ]
    )

    assert "Semester" in extracted["exam_name"]
    assert "Gate" in extracted["exam_name"]
    assert "Github" not in extracted["exam_name"]
    assert "Example" not in extracted["exam_name"]


def test_fallback_extracts_problem_topic_and_one_word_chapter_choice() -> None:
    extractor = EdTechExtractor.__new__(EdTechExtractor)

    extracted = extractor._fallback_extract_from_user_text(
        [
            {"role": "user", "content": "hey"},
            {"role": "assistant", "content": "Which chapter is troubling you?"},
            {"role": "user", "content": "i have problem in physical chemestry"},
            {"role": "assistant", "content": "Thermodynamics, Equilibrium, Electrochemistry, or something else?"},
            {"role": "user", "content": "thermodynamics"},
        ]
    )

    assert any(topic["topic"] == "Physical Chemestry" for topic in extracted["weak_topics"])
    assert extracted["last_topic_studied"] == "Physical Chemestry, Thermodynamics"
