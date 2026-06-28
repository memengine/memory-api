from __future__ import annotations

from pathlib import Path

import pytest

from api.services.extraction_eval_harness import GoldenExpectedMemory
from api.services.extraction_eval_harness import compare_expected_memories
from api.services.extraction_eval_harness import load_golden_extraction_cases
from api.services.extraction_eval_runner import run_golden_extraction_baseline
from api.services.extraction_service import DEFAULT_CONFIDENCE_THRESHOLD


GOLDEN_CASES_DIR = Path(__file__).resolve().parents[1] / "evals" / "general_extraction_cases"


def test_golden_extraction_dataset_has_required_coverage() -> None:
    cases = load_golden_extraction_cases(GOLDEN_CASES_DIR)

    assert len(cases) >= 16
    assert len({case.id for case in cases}) == len(cases)

    case_types = {case.case_type for case in cases}
    assert {"positive", "negative", "composite", "borderline"} <= case_types

    categories = {
        memory.category
        for case in cases
        for memory in case.expected_memories
    }
    assert {
        "preference",
        "fact",
        "goal",
        "procedure",
        "relationship",
        "expertise",
    } <= categories


def test_negative_cases_expect_nothing_to_extract() -> None:
    cases = load_golden_extraction_cases(GOLDEN_CASES_DIR)
    negative_cases = [case for case in cases if case.case_type == "negative"]

    assert negative_cases
    for case in negative_cases:
        assert case.expected_nothing_to_extract is True
        assert case.expected_memories == []


def test_composite_cases_cover_multi_turn_multi_memory_extraction() -> None:
    cases = load_golden_extraction_cases(GOLDEN_CASES_DIR)
    composite_cases = [case for case in cases if case.case_type == "composite"]

    assert composite_cases
    for case in composite_cases:
        assert len(case.messages) >= 3
        assert len(case.expected_memories) >= 2


def test_borderline_cases_capture_threshold_cliff_candidates() -> None:
    cases = load_golden_extraction_cases(GOLDEN_CASES_DIR)
    borderline_cases = [case for case in cases if case.case_type == "borderline"]

    assert borderline_cases
    assert any(
        memory.confidence < DEFAULT_CONFIDENCE_THRESHOLD
        for case in borderline_cases
        for memory in case.expected_memories
    )


def test_compare_expected_memories_reports_pass_and_failures() -> None:
    expected = [
        GoldenExpectedMemory(
            content="User prefers concise Python-first coding explanations over long theory.",
            category="preference",
            confidence=0.92,
            importance_score=7.5,
        )
    ]
    actual = [
        {
            "content": "User prefers concise Python-first coding explanations over long theory.",
            "category": "preference",
        }
    ]

    assert compare_expected_memories(actual, expected).passed is True

    missing = compare_expected_memories([], expected)
    assert missing.passed is False
    assert missing.missing == [expected[0].content]

    mismatched = compare_expected_memories(
        [{"content": expected[0].content, "category": "fact"}],
        expected,
    )
    assert mismatched.passed is False
    assert mismatched.mismatched == [f"{expected[0].content}: category"]

@pytest.mark.asyncio
async def test_golden_baseline_runner_validates_current_parser_and_thresholds() -> None:
    cases = load_golden_extraction_cases(GOLDEN_CASES_DIR)

    summary = await run_golden_extraction_baseline(cases)

    assert summary.total_cases == len(cases)
    assert summary.failed_cases == 0
    assert summary.passed_cases == len(cases)
    assert summary.by_case_type["positive"]["passed"] >= 1
    assert summary.by_case_type["negative"]["passed"] >= 1
    assert summary.by_case_type["composite"]["passed"] >= 1
    assert summary.by_case_type["borderline"]["passed"] >= 1

    negative_results = [result for result in summary.results if result.case_type == "negative"]
    assert negative_results
    assert all(result.nothing_to_extract for result in negative_results)
    assert all(result.extracted_count == 0 for result in negative_results)

    borderline_results = [result for result in summary.results if result.case_type == "borderline"]
    assert borderline_results
    assert any(result.borderline_candidate_count > 0 for result in borderline_results)
    assert any(result.pending_candidates_count > 0 for result in borderline_results)




