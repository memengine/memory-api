from __future__ import annotations

from pathlib import Path

from api.services.edtech.edtech_extractor import EdTechExtractor
from api.services.edtech.edtech_extractor import _merge_extracted_payload
from api.services.edtech.eval_harness import SEMANTIC_FIELDS
from api.services.edtech.eval_harness import compare_expected_subset
from api.services.edtech.eval_harness import load_eval_cases


EVAL_DIR = Path("tests/evals/edtech_cases")


def test_edtech_eval_cases_are_loadable() -> None:
    cases = load_eval_cases(EVAL_DIR)

    assert len(cases) >= 5
    assert {case.learner_type for case in cases} >= {
        "school_student",
        "higher_education",
        "competitive_exam",
        "skill_learner",
    }


def test_deterministic_fallback_only_extracts_safe_facts_from_eval_cases() -> None:
    extractor = EdTechExtractor.__new__(EdTechExtractor)

    for case in load_eval_cases(EVAL_DIR):
        actual = extractor._fallback_extract_from_user_text(case.messages, learner_type=case.learner_type)
        comparison = compare_expected_subset(actual, case.expected_safe_fallback)

        assert comparison.passed, f"{case.id}: missing={comparison.missing}, mismatched={comparison.mismatched}"
        assert not (set(actual) & SEMANTIC_FIELDS), (
            f"{case.id}: fallback emitted semantic fields {set(actual) & SEMANTIC_FIELDS}; "
            "semantic learner state must come from LLM structured extraction."
        )


def test_model_structured_payloads_can_represent_eval_expectations() -> None:
    for case in load_eval_cases(EVAL_DIR):
        normalized = _merge_extracted_payload({}, case.expected_model_extracted)

        assert normalized, f"{case.id}: normalized model payload is empty"
        assert normalized.get("learner_type", case.learner_type) == case.learner_type
