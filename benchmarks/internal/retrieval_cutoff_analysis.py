from __future__ import annotations

import argparse
import json
import math
import statistics
import time
from pathlib import Path
from typing import Any

from benchmarks.internal.retrieval_cases import load_retrieval_development_cases
from api.services.retriever import RetrieverService


CANDIDATE_CUTOFFS = (0.28, 0.30, 0.315, 0.34, 0.40)


def _equivalent_key(case: Any, key: str) -> str:
    if key in case.relevant:
        return key
    returned = next((item for item in case.candidates if item["key"] == key), None)
    if returned is None:
        return key
    for relevant_key in case.relevant:
        expected = next(item for item in case.candidates if item["key"] == relevant_key)
        if RetrieverService._content_similarity(returned["content"], expected["content"]) > 0.95:
            return relevant_key
    return key


def _case_metrics(case: Any, results: list[dict[str, Any]]) -> dict[str, float]:
    keys = [_equivalent_key(case, item["key"]) for item in results]
    relevant = set(case.relevant)
    hits = [key for key in keys if key in relevant]
    precision = len(hits) / len(keys) if keys else (1.0 if not relevant else 0.0)
    recall = len(set(hits)) / len(relevant) if relevant else (1.0 if not keys else 0.0)
    mrr = next(
        (1.0 / rank for rank, key in enumerate(keys, 1) if key in relevant),
        1.0 if not relevant and not keys else 0.0,
    )
    gains = [case.relevant.get(key, 0) for key in keys]
    dcg = sum((2**gain - 1) / math.log2(rank + 1) for rank, gain in enumerate(gains, 1))
    ideal = sorted(case.relevant.values(), reverse=True)[: case.limit]
    idcg = sum((2**gain - 1) / math.log2(rank + 1) for rank, gain in enumerate(ideal, 1))
    return {
        "precision_at_k": precision,
        "recall_at_k": recall,
        "mrr": mrr,
        "ndcg_at_k": dcg / idcg if idcg else 1.0,
    }


def _distribution(values: list[float]) -> dict[str, float | int | None]:
    if not values:
        return {"count": 0, "min": None, "median": None, "max": None}
    return {
        "count": len(values),
        "min": min(values),
        "median": statistics.median(values),
        "max": max(values),
    }


def analyze(capture: dict[str, Any]) -> dict[str, Any]:
    cases = {case.id: case for case in load_retrieval_development_cases()}
    labelled: list[dict[str, Any]] = []
    for row in capture["cases"]:
        case = cases[row["id"]]
        for result in row["result_scores"]:
            equivalent = _equivalent_key(case, result["key"])
            gain = case.relevant.get(equivalent, 0)
            labelled.append(
                {
                    **result,
                    "case_id": case.id,
                    "expected_key": equivalent,
                    "gain": gain,
                    "is_relevant": gain > 0,
                    "is_empty_query": not bool(case.relevant),
                }
            )

    distributions = {
        "clearly_relevant_gain_3": _distribution(
            [item["semantic_score"] for item in labelled if item["gain"] == 3]
        ),
        "weak_acceptable_gain_1_or_2": _distribution(
            [item["semantic_score"] for item in labelled if item["gain"] in {1, 2}]
        ),
        "irrelevant_filler": _distribution(
            [item["semantic_score"] for item in labelled if not item["is_relevant"]]
        ),
        "empty_query_results": _distribution(
            [item["semantic_score"] for item in labelled if item["is_empty_query"]]
        ),
        "top_1": _distribution([item["semantic_score"] for item in labelled if item["rank"] == 1]),
        "lower_ranked": _distribution([item["semantic_score"] for item in labelled if item["rank"] > 1]),
    }

    total_irrelevant = sum(not item["is_relevant"] for item in labelled)
    evaluations = []
    for cutoff in CANDIDATE_CUTOFFS:
        started = time.perf_counter()
        rows = []
        valid_removed = 0
        irrelevant_removed = 0
        for _ in range(10_000):
            for row in capture["cases"]:
                kept = [item for item in row["result_scores"] if item["semantic_score"] >= cutoff]
        elapsed_ms = (time.perf_counter() - started) * 1000 / (10_000 * len(capture["cases"]))
        for row in capture["cases"]:
            case = cases[row["id"]]
            kept = [item for item in row["result_scores"] if item["semantic_score"] >= cutoff]
            removed = [item for item in row["result_scores"] if item["semantic_score"] < cutoff]
            valid_removed += sum(_equivalent_key(case, item["key"]) in case.relevant for item in removed)
            irrelevant_removed += sum(_equivalent_key(case, item["key"]) not in case.relevant for item in removed)
            rows.append(_case_metrics(case, kept))
        relevant_rows = [row for row in capture["cases"] if cases[row["id"]].relevant]
        empty_rows = [row for row in capture["cases"] if not cases[row["id"]].relevant]
        evaluations.append(
            {
                "cutoff": cutoff,
                "precision_at_k": statistics.mean(row["precision_at_k"] for row in rows),
                "aggregate_recall_at_k": statistics.mean(row["recall_at_k"] for row in rows),
                "relevant_case_recall_at_k": statistics.mean(
                    _case_metrics(cases[row["id"]], [
                        item for item in row["result_scores"] if item["semantic_score"] >= cutoff
                    ])["recall_at_k"]
                    for row in relevant_rows
                ),
                "mrr": statistics.mean(row["mrr"] for row in rows),
                "ndcg_at_k": statistics.mean(row["ndcg_at_k"] for row in rows),
                "empty_result_accuracy": statistics.mean(
                    not any(item["semantic_score"] >= cutoff for item in row["result_scores"])
                    for row in empty_rows
                ),
                "valid_memories_incorrectly_removed": valid_removed,
                "irrelevant_results_removed": irrelevant_removed,
                "irrelevant_results_total": total_irrelevant,
                "offline_filter_latency_ms_per_case": elapsed_ms,
            }
        )
    return {
        "source_capture": "retrieval-semantic-cutoff-score-capture-v2.json",
        "holdout_used": False,
        "production_behavior_changed": False,
        "candidate_cutoffs": list(CANDIDATE_CUTOFFS),
        "score_distributions": distributions,
        "evaluations": evaluations,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--capture", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = analyze(json.loads(args.capture.read_text(encoding="utf-8")))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
