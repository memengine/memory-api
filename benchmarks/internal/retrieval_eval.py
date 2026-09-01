from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

from api.services.retriever import RetrieverService
from benchmarks.internal.retrieval_cases import RetrievalCase


MIN_SEMANTIC_SCORE = 0.10


def evaluate_case(case: RetrievalCase) -> dict[str, Any]:
    service = object.__new__(RetrieverService)
    now = datetime.now(UTC)
    points = []
    key_by_id = {}
    for index, candidate in enumerate(case.candidates):
        if candidate.get("active", True) is not True:
            continue
        if case.filters.get("categories") and candidate["category"] not in case.filters["categories"]:
            continue
        if case.filters.get("agent_id") and candidate.get("agent_id") != case.filters["agent_id"]:
            continue
        if case.filters.get("tenant_id") and candidate.get("tenant_id") != case.filters["tenant_id"]:
            continue
        if case.filters.get("max_age_days") is not None and candidate.get("age_days", 0) > case.filters["max_age_days"]:
            continue
        if float(candidate["semantic"]) < MIN_SEMANTIC_SCORE:
            continue
        memory_id = f"00000000-0000-0000-0000-{index + 1:012d}"
        key_by_id[memory_id] = candidate["key"]
        created = now - timedelta(days=float(candidate.get("age_days", 0)))
        points.append(SimpleNamespace(
            id=memory_id, score=float(candidate["semantic"]),
            payload={
                "memory_id": memory_id, "content": candidate["content"],
                "category": candidate["category"], "importance_score": candidate["importance"],
                "confidence_score": 0.95, "created_at": created.isoformat(),
                "last_accessed_at": created.isoformat(), "agent_id": candidate.get("agent_id"),
                "provenance": candidate.get("provenance"), "is_archived": False,
            },
        ))
    results = service._deduplicate_results(service._results_from_qdrant_payloads(points))
    ranked = sorted(results, key=lambda item: item.final_score, reverse=True)[: case.limit]
    retrieved = [key_by_id[item.id] for item in ranked]
    relevant = set(case.relevant)
    hits = [key for key in retrieved if key in relevant]
    precision = len(hits) / len(retrieved) if retrieved else (1.0 if not relevant else 0.0)
    recall = len(hits) / len(relevant) if relevant else (1.0 if not retrieved else 0.0)
    reciprocal_rank = next(
        (1.0 / rank for rank, key in enumerate(retrieved, 1) if key in relevant),
        1.0 if not relevant and not retrieved else 0.0,
    )
    gains = [case.relevant.get(key, 0) for key in retrieved]
    dcg = sum((2**gain - 1) / math.log2(rank + 1) for rank, gain in enumerate(gains, 1))
    ideal = sorted(case.relevant.values(), reverse=True)[: case.limit]
    idcg = sum((2**gain - 1) / math.log2(rank + 1) for rank, gain in enumerate(ideal, 1))
    provenance_ok = all(
        next(candidate for candidate in case.candidates if candidate["key"] == key).get("provenance")
        == result.provenance
        for key, result in zip(retrieved, ranked)
    )
    return {
        "id": case.id, "scenario_type": case.scenario_type, "retrieved": retrieved,
        "expected_relevant": list(case.relevant), "precision_at_k": precision,
        "recall_at_k": recall, "mrr": reciprocal_rank, "ndcg_at_k": dcg / idcg if idcg else 1.0,
        "empty_result_correct": bool(relevant) or not retrieved,
        "superseded_leak": any(not c.get("active", True) and c["key"] in retrieved for c in case.candidates),
        "filter_leak": any(key not in relevant for key in retrieved) if case.scenario_type in {"filter", "isolation", "lifecycle"} else False,
        "duplicate_result_rate": 1.0 - (len(set(retrieved)) / len(retrieved)) if retrieved else 0.0,
        "provenance_preserved": provenance_ok,
    }


def aggregate(rows: list[dict[str, Any]]) -> dict[str, Any]:
    count = len(rows)
    mean = lambda name: sum(float(row[name]) for row in rows) / count
    return {
        "scenario_count": count, "precision_at_k": mean("precision_at_k"),
        "recall_at_k": mean("recall_at_k"), "mrr": mean("mrr"), "ndcg_at_k": mean("ndcg_at_k"),
        "empty_result_accuracy": sum(row["empty_result_correct"] for row in rows) / count,
        "superseded_memory_leakage_rate": sum(row["superseded_leak"] for row in rows) / count,
        "filter_leakage_rate": sum(row["filter_leak"] for row in rows) / count,
        "duplicate_result_rate": mean("duplicate_result_rate"),
        "provenance_preservation": sum(row["provenance_preserved"] for row in rows) / count,
    }
