from __future__ import annotations

from api.services.edtech.learner_type_detector import LearnerTypeDetector


def _messages(text: str) -> list[dict[str, str]]:
    return [{"role": "user", "content": text}]


def test_detects_school_student_from_school_signals() -> None:
    result = LearnerTypeDetector().detect_result(
        _messages("I am in class 12 CBSE and preparing for board exam.")
    )

    assert result.learner_type == "school_student"
    assert result.confidence == "high"


def test_higher_education_wins_over_incidental_project_signals() -> None:
    result = LearnerTypeDetector().detect_result(
        _messages("I am a 2nd year ECE student. I have a GitHub project and semester exam.")
    )

    assert result.learner_type == "higher_education"


def test_detects_higher_education_from_cpi_semester_and_btech_typos() -> None:
    result = LearnerTypeDetector().detect_result(
        _messages("I am doing betech and want to increase CPI in my 3rd sem exam.")
    )

    assert result.learner_type == "higher_education"


def test_detects_competitive_exam_aspirant() -> None:
    result = LearnerTypeDetector().detect_result(
        _messages("I am preparing for SSC CGL Tier 1 and need cut off strategy.")
    )

    assert result.learner_type == "competitive_exam"


def test_detects_professional_cert_candidate() -> None:
    result = LearnerTypeDetector().detect_result(
        _messages("I am taking CFA Level 1. This is my second attempt and I need paper strategy.")
    )

    assert result.learner_type == "professional_cert"


def test_short_signals_do_not_match_inside_normal_words() -> None:
    result = LearnerTypeDetector().detect_result(
        _messages("Can you help me increase my CPI in my 3rd sem exam?")
    )

    assert "ca" not in result.matched_signals["professional_cert"]
    assert result.learner_type == "higher_education"


def test_detects_medical_student() -> None:
    result = LearnerTypeDetector().detect_result(
        _messages("I am an MBBS student preparing for NEET PG while doing hospital rotation.")
    )

    assert result.learner_type == "medical_student"


def test_existing_learner_type_is_sticky() -> None:
    result = LearnerTypeDetector().detect_result(
        _messages("I started coding and pushed a GitHub project."),
        existing_learner_type="school_student",
    )

    assert result.learner_type == "school_student"
    assert result.matched_signals == {"school_student": ["existing_profile"]}
