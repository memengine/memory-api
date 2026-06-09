from __future__ import annotations

import uuid

from api.db.models import EdTechMemory
from api.services.edtech.edtech_extractor import EdTechExtractor
from api.services.edtech.edtech_extractor import _existing_learner_type_for_detection
from api.services.edtech.eval_harness import SEMANTIC_FIELDS


def assert_no_semantic_fallback_fields(extracted: dict) -> None:
    assert not (set(extracted) & SEMANTIC_FIELDS)


def test_fallback_adds_higher_education_context_without_becoming_skill_only() -> None:
    extractor = EdTechExtractor.__new__(EdTechExtractor)

    extracted = extractor._fallback_extract_from_user_text(
        [
            {
                "role": "user",
                "content": (
                    "I am a 2nd year ECE student. I built an ML pipeline on GitHub. "
                    "My semester exam is on 10 July and I am preparing for GATE exam."
                ),
            }
        ],
        learner_type="higher_education",
    )

    assert extracted["grade_level"] == "2nd Year ECE"
    assert extracted["primary_deadline_event"] == extracted["exam_name"]
    assert "Github" not in extracted["exam_name"]
    assert_no_semantic_fallback_fields(extracted)


def test_model_extension_fields_are_normalized_into_context_columns() -> None:
    extractor = EdTechExtractor.__new__(EdTechExtractor)
    memory = EdTechMemory(id=uuid.uuid4(), proxy_user_id=uuid.uuid4(), tenant_id=uuid.uuid4())
    updated: set[str] = set()

    extractor._merge_extracted(
        memory,
        {
            "learner_type": "competitive_exam",
            "deadline": {"event": "UPSC Prelims", "date": "2026-06-15"},
            "exam_details": {"exam_name": "UPSC Prelims", "attempt_number": 2},
            "weak_areas": [{"topic": "Polity", "severity": "moderate"}],
        },
        updated,
    )

    assert memory.learner_type == "competitive_exam"
    assert memory.primary_deadline_event == "UPSC Prelims"
    assert str(memory.primary_deadline_date) == "2026-06-15"
    assert memory.competitive_exam_context["exam_details"]["attempt_number"] == 2
    assert memory.weak_topics[0]["topic"] == "Polity"


def test_fallback_extracts_higher_ed_cpi_semester_and_language_difficulty() -> None:
    extractor = EdTechExtractor.__new__(EdTechExtractor)

    extracted = extractor._fallback_extract_from_user_text(
        [
            {
                "role": "user",
                "content": "I am doing betech from college and want to increase my CPI in semester exam.",
            },
            {"role": "user", "content": "A, because I am poor in English."},
            {"role": "user", "content": "I am currently 3rd sem, and exam on the 10 July."},
        ],
        learner_type="higher_education",
    )

    assert extracted["grade_level"] == "Semester 3"
    assert extracted["primary_deadline_event"] == "Semester"
    assert_no_semantic_fallback_fields(extracted)


def test_empty_profile_does_not_make_bad_learner_type_sticky() -> None:
    memory = EdTechMemory(
        id=uuid.uuid4(),
        proxy_user_id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        learner_type="competitive_exam",
        learner_type_confidence="high",
    )

    assert _existing_learner_type_for_detection(memory) is None

    memory.primary_goal = "Clear SSC CGL"

    assert _existing_learner_type_for_detection(memory) == "competitive_exam"
