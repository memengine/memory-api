from __future__ import annotations

import argparse
import json
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from benchmarks.internal.deterministic_importance import DeterministicImportanceScorer
from benchmarks.internal.live_provider import load_development_cases
from benchmarks.internal.matching import match_memories

FEATURES = (
    "temporal_scope",
    "expertise_maturity",
    "goal_commitment",
    "procedure_durability_consequence",
    "identity_breadth",
    "preference_scope",
    "consequence_of_forgetting",
)


def diagnose(source: Path) -> dict[str, Any]:
    artifact = json.loads(source.read_text(encoding="utf-8"))
    if artifact.get("config", {}).get("holdout_loaded") is not False:
        raise RuntimeError("feature recall diagnosis requires a development-only artifact")
    cases = {case.id: case for case in load_development_cases()}
    scorer = DeterministicImportanceScorer()
    feature_counts = {name: Counter() for name in FEATURES}
    buckets: Counter[str] = Counter()
    category_buckets: dict[str, Counter[str]] = defaultdict(Counter)
    source_buckets: dict[str, Counter[str]] = defaultdict(Counter)
    rows: list[dict[str, Any]] = []

    for artifact_case in artifact["cases"]:
        case = cases[artifact_case["id"]]
        expected = tuple(item for item in case.expected_memories if item.disposition != "discard")
        for match in match_memories(expected, artifact_case["predictions"]):
            wanted = expected[match.expected_index]
            prediction = artifact_case["predictions"][match.actual_index]
            messages = list(case.messages)
            expected_result = scorer.score(
                {"proposition": wanted.proposition, "category": wanted.category, "evidence_turns": list(wanted.evidence_turns)},
                messages,
            )
            predicted_result = scorer.score(prediction, messages)
            expected_features = expected_result.to_dict()["features"]
            predicted_features = predicted_result.to_dict()["features"]
            comparisons = {}
            for name in FEATURES:
                expected_value = int(expected_features[name])
                predicted_value = int(predicted_features[name])
                if expected_value:
                    state = "recalled" if predicted_value == expected_value else "missed" if predicted_value == 0 else "sign_or_level_mismatch"
                else:
                    state = "spurious" if predicted_value else "neutral"
                feature_counts[name][state] += 1
                comparisons[name] = state

            expected_within = wanted.importance_min <= expected_result.score <= wanted.importance_max
            predicted_within = wanted.importance_min <= predicted_result.score <= wanted.importance_max
            if predicted_within:
                bucket = "within_range"
            elif expected_within:
                bucket = "normalization_or_category_loss"
            elif expected_result.score == predicted_result.score:
                bucket = "feature_model_or_annotation_gap"
            else:
                bucket = "mixed_model_and_normalization_gap"
            buckets[bucket] += 1
            category_buckets[wanted.category][bucket] += 1
            source_name = "legacy" if case.source == "legacy-general-extraction-cases" else "modern"
            source_buckets[source_name][bucket] += 1
            rows.append({
                "case_id": case.id,
                "source": source_name,
                "category": wanted.category,
                "expected_proposition": wanted.proposition,
                "predicted_content": prediction.get("content"),
                "importance_range": [wanted.importance_min, wanted.importance_max],
                "expected_proposition_score": expected_result.score,
                "predicted_score": predicted_result.score,
                "expected_features": expected_features,
                "predicted_features": predicted_features,
                "feature_comparison": comparisons,
                "diagnostic_bucket": bucket,
            })

    total = len(rows)
    expected_within_count = sum(
        row["importance_range"][0] <= row["expected_proposition_score"] <= row["importance_range"][1]
        for row in rows
    )
    feature_summary = {}
    for name, counts in feature_counts.items():
        signaled = counts["recalled"] + counts["missed"] + counts["sign_or_level_mismatch"]
        feature_summary[name] = {
            **dict(counts),
            "expected_signal_count": signaled,
            "exact_recall": counts["recalled"] / signaled if signaled else None,
        }
    return {
        "schema_version": "1.0",
        "source_artifact": str(source),
        "scope": "development only",
        "holdout_loaded": False,
        "provider_calls": 0,
        "matched_memory_count": total,
        "canonical_proposition_range_accuracy": expected_within_count / total if total else 1.0,
        "provider_normalized_range_accuracy": buckets["within_range"] / total if total else 1.0,
        "diagnostic_buckets": dict(buckets),
        "buckets_by_category": {key: dict(value) for key, value in sorted(category_buckets.items())},
        "buckets_by_source": {key: dict(value) for key, value in sorted(source_buckets.items())},
        "feature_recall": feature_summary,
        "cases": rows,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Diagnose deterministic feature recall without provider calls.")
    parser.add_argument("source", type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    result = diagnose(args.source)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({key: value for key, value in result.items() if key != "cases"}, indent=2))


if __name__ == "__main__":
    main()
