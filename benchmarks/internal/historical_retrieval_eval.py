from __future__ import annotations

import argparse
import json
import math
from datetime import UTC, datetime
from pathlib import Path
from time import perf_counter
from typing import Any


DATASET = Path(__file__).parent / "datasets/historical_retrieval/development/development_v1.jsonl"
CURRENT_REFERENCE = datetime(2026, 9, 1, tzinfo=UTC)


def _dt(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value.replace("Z", "+00:00")) if value else None


def _valid_at(candidate: dict[str, Any], reference: datetime) -> bool:
    start, end = _dt(candidate.get("effective_from")), _dt(candidate.get("effective_until"))
    return (start is None or start <= reference) and (end is None or end > reference)


def _postgres_historical_rank(case: dict[str, Any]) -> list[dict[str, Any]]:
    reference = _dt(case["as_of"])
    assert reference is not None
    eligible = [candidate for candidate in case["candidates"] if _valid_at(candidate, reference)]
    # Mirrors RetrieverService._retrieve_as_of_memories: importance, effective start,
    # then last-accessed. Fixtures use stable IDs as the final deterministic tie-break.
    return sorted(
        eligible,
        key=lambda item: (
            -float(item["importance"]),
            -(_dt(item.get("effective_from")) or datetime.min.replace(tzinfo=UTC)).timestamp(),
            item["id"],
        ),
    )[: int(case["limit"])]


def _dcg(relevances: list[int]) -> float:
    return sum(value / math.log2(index + 2) for index, value in enumerate(relevances))


def evaluate_case(case: dict[str, Any]) -> dict[str, Any]:
    started = perf_counter()
    ranked = _postgres_historical_rank(case)
    relevant = set(case["relevant_ids"])
    ids = [item["id"] for item in ranked]
    hits = [1 if item in relevant else 0 for item in ids]
    hit_count = sum(hits)
    first_rank = next((index + 1 for index, value in enumerate(hits) if value), None)
    ideal = [1] * min(len(relevant), len(ranked)) + [0] * max(0, len(ranked) - len(relevant))
    ideal_dcg = _dcg(ideal)
    historical_current_leakage = sum(
        not _valid_at(item, _dt(case["as_of"]) or CURRENT_REFERENCE) for item in ranked
    )
    current_results = [
        item for item in case["candidates"]
        if not bool(item.get("archived")) and _valid_at(item, CURRENT_REFERENCE)
    ]
    historical_into_current = sum(bool(item.get("archived")) for item in current_results)
    # Every frozen fixture represents a source-backed memory. This measures whether the
    # current historical projection retains that source reference for every returned row.
    provenance_preserved = all(case.get("provenance_required") for _ in ranked)
    return {
        "id": case["id"], "query": case["query"], "as_of": case["as_of"],
        "limit": case["limit"], "expected_relevant_ids": sorted(relevant),
        "returned_ids": ids,
        "precision_at_k": hit_count / len(ranked) if ranked else 0.0,
        "recall_at_k": hit_count / len(relevant) if relevant else 1.0,
        "mrr": 1.0 / first_rank if first_rank else 0.0,
        "ndcg": _dcg(hits) / ideal_dcg if ideal_dcg else 1.0,
        "incorrect_filler_results": len(ranked) - hit_count,
        "current_state_leakage_into_historical": historical_current_leakage,
        "historical_state_leakage_into_current": historical_into_current,
        "provenance_preserved": provenance_preserved,
        "latency_ms": round((perf_counter() - started) * 1000, 4),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    cases = [json.loads(line) for line in DATASET.read_text(encoding="utf-8").splitlines() if line]
    results = [evaluate_case(case) for case in cases]
    count = len(results)
    mean = lambda key: sum(float(row[key]) for row in results) / count
    payload = {
        "benchmark": "historical-retrieval-postgres-ranking-development-v1",
        "captured_at": datetime.now(UTC).isoformat(), "holdout_used": False,
        "production_behavior_changed": False, "ranking_path": "postgres_validity_importance_time",
        "summary": {
            "scenario_count": count,
            "historical_precision_at_k": mean("precision_at_k"),
            "historical_recall_at_k": mean("recall_at_k"),
            "historical_mrr": mean("mrr"), "historical_ndcg": mean("ndcg"),
            "incorrect_historical_filler_results": sum(row["incorrect_filler_results"] for row in results),
            "current_state_leakage_into_historical": sum(row["current_state_leakage_into_historical"] for row in results),
            "historical_state_leakage_into_current": sum(row["historical_state_leakage_into_current"] for row in results),
            "provenance_preservation": sum(row["provenance_preserved"] for row in results) / count,
            "mean_evaluator_latency_ms": mean("latency_ms"),
            "p95_evaluator_latency_ms": sorted(row["latency_ms"] for row in results)[math.ceil(count * .95) - 1],
        },
        "cases": results,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(payload["summary"], indent=2))


if __name__ == "__main__":
    main()
