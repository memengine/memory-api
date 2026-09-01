from __future__ import annotations

from typing import Any

from benchmarks.internal.cases import ExtractionCase
from benchmarks.internal.metrics import evaluate_extraction
from benchmarks.internal.results import build_run_record


def evaluate_predictions(
    cases: list[ExtractionCase],
    predictions_by_case: dict[str, list[dict[str, Any]]],
    *,
    costs_by_case: dict[str, float] | None = None,
    config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Evaluate precomputed outputs so provider execution stays outside metric logic."""
    costs = costs_by_case or {}
    metrics = [
        evaluate_extraction(
            case,
            predictions_by_case.get(case.id, []),
            estimated_cost_usd=costs.get(case.id, 0.0),
        )
        for case in cases
    ]
    return build_run_record(cases, metrics, config=config)


def expected_output_predictions(cases: list[ExtractionCase]) -> dict[str, list[dict[str, Any]]]:
    """Deterministic evaluator contract fixture; never a model-quality baseline."""
    return {
        case.id: [
            {
                "content": memory.proposition,
                "category": memory.category,
                "disposition": memory.disposition,
                "importance_score": (memory.importance_min + memory.importance_max) / 2,
                "confidence": (memory.confidence_min + memory.confidence_max) / 2,
                "evidence_turns": list(memory.evidence_turns),
            }
            for memory in case.expected_memories
            if memory.disposition != "discard"
        ]
        for case in cases
    }
