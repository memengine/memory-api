from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from api.services.extraction_eval_harness import GoldenComparison
from api.services.extraction_eval_harness import GoldenExtractionCase
from api.services.extraction_eval_harness import GoldenExpectedMemory
from api.services.extraction_eval_harness import compare_expected_memories
from api.services.extraction_service import DEFAULT_CONFIDENCE_THRESHOLD
from api.services.extraction_service import ExtractionService
from api.services.llm_service import LLMResponse


MIN_STORED_IMPORTANCE = 2.0


class StaticGoldenLLMService:
    """Deterministic LLM stub for golden extraction validation."""

    def __init__(self, content: str) -> None:
        self.content = content
        self.calls: list[dict[str, Any]] = []

    async def complete(self, **kwargs: Any) -> LLMResponse:
        self.calls.append(kwargs)
        return LLMResponse(
            content=self.content,
            provider_used="golden",
            model_used="golden-expected-output",
            input_tokens=0,
            output_tokens=0,
            total_tokens=0,
            latency_ms=0,
        )


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

    The baseline intentionally uses expected memories as the mocked model output.
    This isolates parser, threshold, and nothing-to-extract behavior before we
    spend tokens on model quality evaluation.
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
        bucket = by_case_type.setdefault(result.case_type, {"total": 0, "passed": 0, "failed": 0})
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
    llm = StaticGoldenLLMService(_expected_llm_payload(case))
    service = ExtractionService(
        llm_service=llm,
        spec_path=spec_path,
        confidence_threshold=confidence_threshold,
    )
    extraction = await service.extract(
        messages=case.messages,
        proxy_user_id=f"golden-{case.id}",
        tenant_id="golden-eval",
        job_id=f"golden-{case.id}",
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
        for memory in extraction.memories_to_store
    ]
    comparison = compare_expected_memories(actual, expected_stored)
    nothing_matches = extraction.nothing_to_extract is case.expected_nothing_to_extract

    return GoldenBaselineCaseResult(
        case_id=case.id,
        case_type=case.case_type,
        passed=comparison.passed and nothing_matches,
        expected_stored_count=len(expected_stored),
        extracted_count=extraction.memories_extracted,
        filtered_count=extraction.memories_filtered,
        pending_candidates_count=extraction.pending_candidates_count,
        borderline_candidate_count=sum(
            1 for memory in case.expected_memories if memory.confidence < confidence_threshold
        ),
        nothing_to_extract=extraction.nothing_to_extract,
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
