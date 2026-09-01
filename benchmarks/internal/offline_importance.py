from __future__ import annotations

import argparse
import copy
import json
from collections import Counter, defaultdict
from dataclasses import asdict
from pathlib import Path
from typing import Any

from benchmarks.internal.cases import ExtractionCase
from benchmarks.internal.deterministic_importance import DeterministicImportanceScorer
from benchmarks.internal.live_provider import load_development_cases
from benchmarks.internal.matching import match_memories
from benchmarks.internal.metrics import aggregate_metrics, evaluate_extraction


def rescore_development_artifact(source: Path) -> dict[str, Any]:
    cases = {case.id: case for case in load_development_cases()}
    record = copy.deepcopy(json.loads(source.read_text(encoding="utf-8")))
    if record.get("config", {}).get("holdout_loaded") is not False:
        raise RuntimeError("offline importance evaluation requires a development-only artifact")

    scorer = DeterministicImportanceScorer()
    metrics = []
    feature_counts: Counter[str] = Counter()
    unchanged = True
    for row in record["cases"]:
        case = cases[row["id"]]
        predictions = row["predictions"]
        snapshot = [
            {key: value for key, value in prediction.items() if key != "importance_score"}
            for prediction in predictions
        ]
        scoring_details = []
        for prediction in predictions:
            result = scorer.score(prediction, list(case.messages))
            prediction["importance_score"] = result.score
            scoring_details.append(result.to_dict())
            for name, enabled in asdict(result.features).items():
                if enabled is True:
                    feature_counts[name] += 1
        unchanged &= snapshot == [
            {key: value for key, value in prediction.items() if key != "importance_score"}
            for prediction in predictions
        ]
        row["deterministic_importance"] = scoring_details
        metric = evaluate_extraction(
            case,
            predictions,
            estimated_cost_usd=float(row["metrics"].get("estimated_cost_usd", 0.0)),
        )
        row["metrics"] = asdict(metric)
        metrics.append(metric)

    record["summary"].update(aggregate_metrics(metrics))
    record["summary"]["deterministic_importance"] = _diagnostics(record["cases"], cases)
    record["summary"]["deterministic_importance"].update(
        {
            "output_unchanged": unchanged,
            "feature_counts": dict(feature_counts),
            "provider_calls": 0,
            "latency_ms": 0,
            "tokens": 0,
            "estimated_cost_usd": 0.0,
        }
    )
    record["config"]["mode"] = "offline-deterministic-importance-development-only"
    record["config"]["rescored_from"] = str(source)
    record["config"]["holdout_loaded"] = False
    return record


def _diagnostics(rows: list[dict[str, Any]], cases: dict[str, ExtractionCase]) -> dict[str, Any]:
    statuses: Counter[str] = Counter()
    distribution: Counter[str] = Counter()
    category_counts: dict[str, Counter[str]] = defaultdict(Counter)
    source_counts: dict[str, Counter[str]] = defaultdict(Counter)
    errors: list[float] = []
    matched = 0
    for row in rows:
        case = cases[row["id"]]
        expected = tuple(item for item in case.expected_memories if item.disposition != "discard")
        for match in match_memories(expected, row["predictions"]):
            wanted = expected[match.expected_index]
            score = float(row["predictions"][match.actual_index]["importance_score"])
            status = "within"
            error = 0.0
            if score < wanted.importance_min:
                status = "under"
                error = wanted.importance_min - score
            elif score > wanted.importance_max:
                status = "over"
                error = score - wanted.importance_max
            statuses[status] += 1
            distribution[str(score)] += 1
            category_counts[wanted.category][status] += 1
            source = "legacy" if case.source == "legacy-general-extraction-cases" else "modern"
            source_counts[source][status] += 1
            errors.append(error)
            matched += 1

    def accuracy(counts: Counter[str]) -> float:
        total = sum(counts.values())
        return counts["within"] / total if total else 1.0

    return {
        "matched_count": matched,
        "importance_accuracy": accuracy(statuses),
        "under_rate": statuses["under"] / matched if matched else 0.0,
        "over_rate": statuses["over"] / matched if matched else 0.0,
        "mean_outside_range_error": sum(errors) / len(errors) if errors else 0.0,
        "score_distribution": dict(sorted(distribution.items(), key=lambda item: float(item[0]))),
        "distinct_score_count": len(distribution),
        "score_5_share": distribution["5.0"] / matched if matched else 0.0,
        "accuracy_by_category": {
            category: accuracy(counts) for category, counts in sorted(category_counts.items())
        },
        "accuracy_by_source": {
            source: accuracy(counts) for source, counts in sorted(source_counts.items())
        },
        "status_counts": dict(statuses),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Offline deterministic importance evaluation.")
    parser.add_argument("source", type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    record = rescore_development_artifact(args.source)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({"output": str(args.output), "summary": record["summary"]}, indent=2))


if __name__ == "__main__":
    main()
