from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from api.services.extraction_eval_harness import (
    GoldenComparison,
    GoldenExpectedMemory,
    GoldenExtractionCase,
    compare_expected_memories,
)
from api.services.extraction_service import (
    DEFAULT_CONFIDENCE_THRESHOLD,
    ExtractionService,
)

MIN_STORED_IMPORTANCE = 2.0


@dataclass(frozen=True)
class GoldenBaselineCaseResult:
    case_id: str
    case_type: str
    passed: bool
    expected_stored_count: int
    extracted_count: int
    filtered_count: int
    pending_candidates_count: int
    borderline_candidate_count: int
    nothing_to_extract: bool
    comparison: GoldenComparison


@dataclass(frozen=True)
class GoldenBaselineSummary:
    total_cases: int
    passed_cases: int
    failed_cases: int
    by_case_type: dict[str, dict[str, int]]
    results: list[GoldenBaselineCaseResult]


async def run_golden_extraction_baseline(
    cases: list[GoldenExtractionCase],
    *,
    spec_path: str | Path | None = None,
    confidence_threshold: float = DEFAULT_CONFIDENCE_THRESHOLD,
) -> GoldenBaselineSummary:
    """Run golden cases through the real parser/filter path without live LLM calls.

    These historical fixtures contain generated paraphrases, not v2 source
    clauses. Test their legacy parser and thresholds explicitly; never send
    them through the new provider write contract or claim model accuracy.
    """
    results: list[GoldenBaselineCaseResult] = []
    for case in cases:
        result = await _run_case(
            case,
            spec_path=spec_path,
            confidence_threshold=confidence_threshold,
        )
        results.append(result)

    by_case_type: dict[str, dict[str, int]] = {}
    for result in results:
        bucket = by_case_type.setdefault(
            result.case_type, {"total": 0, "passed": 0, "failed": 0}
        )
        bucket["total"] += 1
        if result.passed:
            bucket["passed"] += 1
        else:
            bucket["failed"] += 1

    passed_cases = sum(1 for result in results if result.passed)
    return GoldenBaselineSummary(
        total_cases=len(results),
        passed_cases=passed_cases,
        failed_cases=len(results) - passed_cases,
        by_case_type=by_case_type,
        results=results,
    )


async def _run_case(
    case: GoldenExtractionCase,
    *,
    spec_path: str | Path | None,
    confidence_threshold: float,
) -> GoldenBaselineCaseResult:
    service = ExtractionService(
        client=object(),
        spec_path=spec_path,
        confidence_threshold=confidence_threshold,
        importance_shadow_enabled=False,
        proposal_confirmation_enabled=False,
        app_env="test",
    )
    kept, pending, filtered_count, nothing_to_extract, _rejections = (
        service._parse_and_validate_response(
            _expected_llm_payload(case),
            messages=case.messages,
            evidence_context={"extractor_version": "legacy-parser-golden-v1"},
        )
    )

    expected_stored = _expected_stored_memories(
        case.expected_memories,
        confidence_threshold=confidence_threshold,
    )
    actual = [
        {
            "content": memory.content,
            "category": memory.category,
        }
        for memory in kept
    ]
    comparison = compare_expected_memories(actual, expected_stored)
    nothing_matches = nothing_to_extract is case.expected_nothing_to_extract

    return GoldenBaselineCaseResult(
        case_id=case.id,
        case_type=case.case_type,
        passed=comparison.passed and nothing_matches,
        expected_stored_count=len(expected_stored),
        extracted_count=len(kept),
        filtered_count=filtered_count,
        pending_candidates_count=len(pending),
        borderline_candidate_count=sum(
            1
            for memory in case.expected_memories
            if memory.confidence < confidence_threshold
        ),
        nothing_to_extract=nothing_to_extract,
        comparison=comparison,
    )


def _expected_llm_payload(case: GoldenExtractionCase) -> str:
    return json.dumps(
        {
            "memories": [
                {
                    "content": memory.content,
                    "category": memory.category,
                    "importance_score": memory.importance_score,
                    "confidence": memory.confidence,
                    "evidence_turns": list(memory.evidence_turns),
                    "evidence_relation": memory.evidence_relation,
                    "reasoning": "Golden expected memory.",
                }
                for memory in case.expected_memories
            ],
            "nothing_to_extract": case.expected_nothing_to_extract,
            "extraction_notes": case.notes,
        }
    )


def _expected_stored_memories(
    memories: list[GoldenExpectedMemory],
    *,
    confidence_threshold: float,
) -> list[GoldenExpectedMemory]:
    return [
        memory
        for memory in memories
        if memory.confidence >= confidence_threshold
        and memory.importance_score >= MIN_STORED_IMPORTANCE
    ]
