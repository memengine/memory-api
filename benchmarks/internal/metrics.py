from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

from benchmarks.internal.cases import ExtractionCase
from benchmarks.internal.matching import match_memories


@dataclass(frozen=True)
class ExtractionMetrics:
    expected_count: int
    predicted_count: int
    matched_count: int
    precision: float
    recall: float
    f1: float
    category_accuracy: float
    disposition_accuracy: float
    importance_range_accuracy: float
    confidence_range_accuracy: float
    evidence_accuracy: float
    evidence_annotated_count: int
    missing_evidence_annotation_count: int
    strict_match_count: int
    relaxed_match_count: int
    ambiguous_category_match_count: int
    false_memory_rate: float
    forbidden_leak_count: int
    forbidden_leak_rate: float
    confidence_brier_score: float
    confidence_ece: float
    estimated_cost_usd: float

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def evaluate_extraction(
    case: ExtractionCase,
    predictions: list[dict[str, Any]],
    *,
    estimated_cost_usd: float = 0.0,
) -> ExtractionMetrics:
    expected = tuple(item for item in case.expected_memories if item.disposition != "discard")
    matches = match_memories(expected, predictions)
    true_positive = len(matches)
    precision = _ratio(true_positive, len(predictions), empty=1.0)
    recall = _ratio(true_positive, len(expected), empty=1.0)
    f1 = _ratio(2 * precision * recall, precision + recall, empty=0.0)
    category_correct = disposition_correct = importance_correct = confidence_correct = 0
    evidence_correct = evidence_total = 0
    ambiguous_category_matches = 0
    brier_terms: list[float] = []
    calibration_points: list[tuple[float, int]] = []
    matched_actual = {match.actual_index for match in matches}
    for match in matches:
        wanted = expected[match.expected_index]
        actual = predictions[match.actual_index]
        actual_category = str(actual.get("category", "")).lower()
        accepted_categories = {wanted.category, *wanted.acceptable_categories}
        category_correct += actual_category in accepted_categories
        ambiguous_category_matches += (
            actual_category != wanted.category
            and actual_category in wanted.acceptable_categories
        )
        disposition_correct += str(actual.get("disposition", "store")).lower() == wanted.disposition
        importance_correct += _in_range(actual.get("importance_score"), wanted.importance_min, wanted.importance_max)
        confidence_correct += _in_range(actual.get("confidence"), wanted.confidence_min, wanted.confidence_max)
        if wanted.evidence_turns:
            evidence_total += 1
            actual_turns = {int(item) for item in actual.get('evidence_turns', [])}
            evidence_correct += actual_turns == set(wanted.evidence_turns)
        confidence = _bounded(actual.get('confidence', 0.0))
        brier_terms.append((confidence - 1.0) ** 2)
        calibration_points.append((confidence, 1))
    for index, actual in enumerate(predictions):
        if index not in matched_actual:
            confidence = _bounded(actual.get('confidence', 0.0))
            brier_terms.append(confidence**2)
            calibration_points.append((confidence, 0))
    combined_content = "\n".join(str(item.get("content", "")).lower() for item in predictions)
    leaks = sum(pattern in combined_content for pattern in case.forbidden_patterns)
    return ExtractionMetrics(
        expected_count=len(expected),
        predicted_count=len(predictions),
        matched_count=true_positive,
        precision=precision,
        recall=recall,
        f1=f1,
        category_accuracy=_ratio(category_correct, true_positive, empty=1.0),
        disposition_accuracy=_ratio(disposition_correct, true_positive, empty=1.0),
        importance_range_accuracy=_ratio(importance_correct, true_positive, empty=1.0),
        confidence_range_accuracy=_ratio(confidence_correct, true_positive, empty=1.0),
        evidence_accuracy=_ratio(evidence_correct, evidence_total, empty=1.0),
        evidence_annotated_count=evidence_total,
        missing_evidence_annotation_count=sum(not item.evidence_turns for item in expected),
        strict_match_count=sum(match.method == "strict" for match in matches),
        relaxed_match_count=sum(match.method != "strict" for match in matches),
        ambiguous_category_match_count=ambiguous_category_matches,
        false_memory_rate=_ratio(len(predictions) - true_positive, len(predictions), empty=0.0),
        forbidden_leak_count=leaks,
        forbidden_leak_rate=_ratio(leaks, len(case.forbidden_patterns), empty=0.0),
        confidence_brier_score=_ratio(sum(brier_terms), len(brier_terms), empty=0.0),
        confidence_ece=_expected_calibration_error(calibration_points),
        estimated_cost_usd=float(estimated_cost_usd),
    )


def aggregate_metrics(results: list[ExtractionMetrics]) -> dict[str, Any]:
    expected = sum(item.expected_count for item in results)
    predicted = sum(item.predicted_count for item in results)
    matched = sum(item.matched_count for item in results)
    precision = _ratio(matched, predicted, empty=1.0)
    recall = _ratio(matched, expected, empty=1.0)
    summary: dict[str, Any] = {
        "case_count": len(results),
        "expected_count": expected,
        "predicted_count": predicted,
        "matched_count": matched,
        "micro_precision": precision,
        "micro_recall": recall,
        "micro_f1": _ratio(2 * precision * recall, precision + recall, empty=0.0),
        "macro_f1": _ratio(sum(item.f1 for item in results), len(results), empty=0.0),
        "category_accuracy": _weighted(results, "category_accuracy", "matched_count"),
        "disposition_accuracy": _weighted(results, "disposition_accuracy", "matched_count"),
        "importance_range_accuracy": _weighted(results, "importance_range_accuracy", "matched_count"),
        "confidence_range_accuracy": _weighted(results, "confidence_range_accuracy", "matched_count"),
        "evidence_accuracy": _weighted(results, "evidence_accuracy", "evidence_annotated_count"),
        "evidence_annotated_count": sum(item.evidence_annotated_count for item in results),
        "false_memory_rate": _ratio(predicted - matched, predicted, empty=0.0),
        "forbidden_leak_count": sum(item.forbidden_leak_count for item in results),
        "confidence_brier_score": _ratio(sum(item.confidence_brier_score for item in results), len(results), empty=0.0),
        "confidence_ece": _ratio(sum(item.confidence_ece for item in results), len(results), empty=0.0),
        "estimated_cost_usd": sum(item.estimated_cost_usd for item in results),
        "evaluator_diagnostics": {
            "strict_match_count": sum(item.strict_match_count for item in results),
            "relaxed_match_count": sum(item.relaxed_match_count for item in results),
            "ambiguous_category_match_count": sum(
                item.ambiguous_category_match_count for item in results
            ),
            "missing_evidence_annotation_count": sum(
                item.missing_evidence_annotation_count for item in results
            ),
        },
    }
    summary["metric_groups"] = {
        "extraction_quality": {
            key: summary[key]
            for key in (
                "micro_precision",
                "micro_recall",
                "micro_f1",
                "macro_f1",
                "category_accuracy",
                "disposition_accuracy",
                "false_memory_rate",
                "forbidden_leak_count",
            )
        },
        "calibration": {
            key: summary[key]
            for key in (
                "importance_range_accuracy",
                "confidence_range_accuracy",
                "confidence_brier_score",
                "confidence_ece",
            )
        },
        "evidence_provenance": {
            key: summary[key]
            for key in ("evidence_accuracy", "evidence_annotated_count")
        },
        "evaluator_annotation_diagnostics": summary["evaluator_diagnostics"],
    }
    return summary


def _weighted(results: list[ExtractionMetrics], value: str, weight: str) -> float:
    total = sum(getattr(item, weight) for item in results)
    return _ratio(sum(getattr(item, value) * getattr(item, weight) for item in results), total, empty=1.0)


def _ratio(numerator: float, denominator: float, *, empty: float) -> float:
    return numerator / denominator if denominator else empty


def _in_range(value: Any, low: float, high: float) -> bool:
    try:
        return low <= float(value) <= high
    except (TypeError, ValueError):
        return False


def _bounded(value: Any) -> float:
    try:
        return max(0.0, min(1.0, float(value)))
    except (TypeError, ValueError):
        return 0.0


def _expected_calibration_error(points: list[tuple[float, int]], bins: int = 10) -> float:
    if not points:
        return 0.0
    total_error = 0.0
    for index in range(bins):
        low, high = index / bins, (index + 1) / bins
        bucket = [
            point
            for point in points
            if low <= point[0] < high or (index == bins - 1 and point[0] == 1.0)
        ]
        if bucket:
            mean_confidence = sum(point[0] for point in bucket) / len(bucket)
            accuracy = sum(point[1] for point in bucket) / len(bucket)
            total_error += (len(bucket) / len(points)) * abs(mean_confidence - accuracy)
    return total_error
