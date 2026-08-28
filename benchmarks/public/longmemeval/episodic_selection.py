from __future__ import annotations

import argparse
import json
import re
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from qdrant_client import QdrantClient, models

from api.services.embedding_service import EmbeddingService
from benchmarks.public.longmemeval.contract import (
    LongMemEvalCase,
    load_dataset,
    select_stratified_smoke_cases,
)
from benchmarks.public.longmemeval.episodic_shadow import (
    COLLECTION_PREFIX,
    EMBEDDING_PRICE_USD_PER_MILLION,
    FROZEN_QUESTION_IDS,
    SEMANTIC_FLOOR,
    _safe_local_qdrant_url,
    _token_count,
    build_records,
    ranking_metrics,
)

CANDIDATE_CUTOFFS = (0.315, 0.35, 0.375, 0.4)
MONTHS = {
    "january": 1,
    "february": 2,
    "march": 3,
    "april": 4,
    "may": 5,
    "june": 6,
    "july": 7,
    "august": 8,
    "september": 9,
    "october": 10,
    "november": 11,
    "december": 12,
}


def decompose_comparison_query(question: str) -> list[str]:
    """Return explicit alternatives without inferring or rewriting their meaning."""
    normalized = question.strip()
    match = re.search(
        r"\b(?:which|what)\b.*?\bfirst\b\s*,\s*(.+?)\s+or\s+(.+?)[?.!]*$",
        normalized,
        flags=re.IGNORECASE,
    )
    if not match:
        return [normalized]
    variants = [normalized, match.group(1).strip(), match.group(2).strip()]
    return list(dict.fromkeys(variant for variant in variants if variant))


def explicit_month_constraint(question: str) -> int | None:
    lowered = question.lower()
    found = [number for name, number in MONTHS.items() if re.search(rf"\b{name}\b", lowered)]
    return found[0] if len(found) == 1 else None


def record_supports_month(text: str, month: int) -> bool:
    month_name = next(name for name, number in MONTHS.items() if number == month)
    if re.search(rf"\b{month_name}\b", text, flags=re.IGNORECASE):
        return True
    return bool(re.search(rf"(?<!\d)0?{month}/(?:0?[1-9]|[12]\d|3[01])(?!\d)", text))


def _aggregate(rows: list[dict[str, Any]], metric: str) -> float:
    return sum(float(row["metrics"][metric]) for row in rows) / len(rows)


def _evaluate_candidate(
    cases: list[LongMemEvalCase],
    scored: dict[str, list[dict[str, Any]]],
    cutoff: float,
) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    removed_valid = 0
    removed_irrelevant = 0
    for case in cases:
        expected = case.answer_session_ids
        expected_set = set(expected)
        all_results = scored[case.question_id]
        kept = [row for row in all_results if row["score"] >= cutoff]
        removed = [row for row in all_results if row["score"] < cutoff]
        removed_valid += sum(row["session_id"] in expected_set for row in removed)
        removed_irrelevant += sum(row["session_id"] not in expected_set for row in removed)
        ids = [row["session_id"] for row in kept]
        rows.append(
            {
                "question_id": case.question_id,
                "abstention": case.question_id.endswith("_abs"),
                "result_session_ids": ids,
                "metrics": ranking_metrics(ids, expected),
            }
        )
    answerable = [row for row in rows if not row["abstention"]]
    abstentions = [row for row in rows if row["abstention"]]
    return {
        "cutoff": cutoff,
        "precision_at_k": _aggregate(rows, "precision_at_k"),
        "answerable_precision_at_k": _aggregate(answerable, "precision_at_k"),
        "answerable_recall_at_k": _aggregate(answerable, "recall_at_k"),
        "abstention_evidence_precision_at_k": _aggregate(
            abstentions, "precision_at_k"
        ),
        "abstention_evidence_recall_at_k": _aggregate(abstentions, "recall_at_k"),
        "mrr": _aggregate(rows, "mrr"),
        "ndcg": _aggregate(rows, "ndcg"),
        "abstention_empty_result_rate": sum(
            not row["result_session_ids"] for row in abstentions
        ) / len(abstentions),
        "valid_memories_incorrectly_removed": removed_valid,
        "irrelevant_results_removed": removed_irrelevant,
        "irrelevant_filler_results": sum(
            int(row["metrics"]["irrelevant_filler_results"]) for row in rows
        ),
        "cases": rows,
    }


def run(args: argparse.Namespace) -> dict[str, Any]:
    if not args.approve_embedding_calls:
        raise SystemExit("refusing provider calls without --approve-embedding-calls")
    cases, dataset_sha256 = load_dataset(args.dataset)
    cases = select_stratified_smoke_cases(cases)
    if {case.question_id for case in cases} != FROZEN_QUESTION_IDS:
        raise SystemExit("frozen six-case selection changed; refusing to run")
    qdrant_url = _safe_local_qdrant_url(args.qdrant_url)
    collection = args.collection or f"{COLLECTION_PREFIX}selection_{uuid.uuid4().hex[:10]}"
    if not collection.startswith(f"{COLLECTION_PREFIX}selection_"):
        raise SystemExit("selection collection must use the disposable shadow prefix")

    records = build_records(cases)
    qdrant = QdrantClient(url=qdrant_url, timeout=20)
    embedder = EmbeddingService()
    input_tokens = 0
    query_vectors = 0
    dimensions = 0
    model_id = ""
    cleanup = {"attempted": False, "succeeded": False}
    scored: dict[str, list[dict[str, Any]]] = {}
    retrieval_latencies: list[float] = []
    index_started = time.perf_counter()
    try:
        for index, record in enumerate(records):
            embedded = embedder.embed_sync(record.text)
            dimensions = embedded.dimensions
            model_id = embedded.model_id
            input_tokens += _token_count(record.text)
            if index == 0:
                qdrant.create_collection(
                    collection_name=collection,
                    vectors_config=models.VectorParams(
                        size=dimensions, distance=models.Distance.COSINE
                    ),
                )
                qdrant.create_payload_index(
                    collection_name=collection,
                    field_name="question_id",
                    field_schema=models.PayloadSchemaType.KEYWORD,
                    wait=True,
                )
            qdrant.upsert(
                collection_name=collection,
                points=[
                    models.PointStruct(
                        id=record.point_id,
                        vector=embedded.vector,
                        payload={
                            "question_id": record.question_id,
                            "session_id": record.session_id,
                            "occurred_at": record.occurred_at,
                            "roles": list(record.roles),
                            "turn_count": record.turn_count,
                            "content_hash": record.content_hash,
                        },
                    )
                ],
                wait=True,
            )
        indexing_latency_ms = (time.perf_counter() - index_started) * 1000

        for case in cases:
            started = time.perf_counter()
            best: dict[str, dict[str, Any]] = {}
            variants = decompose_comparison_query(case.question)
            for variant_index, variant in enumerate(variants):
                embedded = embedder.embed_sync(variant)
                query_vectors += 1
                input_tokens += _token_count(variant)
                response = qdrant.query_points(
                    collection_name=collection,
                    query=embedded.vector,
                    query_filter=models.Filter(
                        must=[
                            models.FieldCondition(
                                key="question_id",
                                match=models.MatchValue(value=case.question_id),
                            )
                        ]
                    ),
                    limit=args.limit,
                    score_threshold=SEMANTIC_FLOOR,
                    with_payload=True,
                    with_vectors=False,
                )
                for point in response.points:
                    session_id = str(point.payload["session_id"])
                    current = best.get(session_id)
                    score = float(point.score)
                    if current is None or score > current["score"]:
                        best[session_id] = {
                            "session_id": session_id,
                            "score": score,
                            "matched_query_index": variant_index,
                            "occurred_at": point.payload["occurred_at"],
                            "roles": point.payload["roles"],
                            "turn_count": point.payload["turn_count"],
                            "content_hash": point.payload["content_hash"],
                        }
            values = list(best.values())
            scored[case.question_id] = sorted(
                values, key=lambda row: (-row["score"], row["session_id"])
            )
            retrieval_latencies.append((time.perf_counter() - started) * 1000)

        candidates = [
            _evaluate_candidate(cases, scored, cutoff) for cutoff in CANDIDATE_CUTOFFS
        ]
        passing = [
            candidate
            for candidate in candidates
            if candidate["answerable_recall_at_k"] == 1.0
            and candidate["abstention_evidence_recall_at_k"] == 1.0
            and candidate["valid_memories_incorrectly_removed"] == 0
        ]
        selected = max(
            passing,
            key=lambda candidate: (
                candidate["answerable_precision_at_k"],
                candidate["ndcg"],
                candidate["cutoff"],
            ),
            default=None,
        )
        return {
            "schema_version": "longmemeval-episodic-selection-v1",
            "created_at": datetime.now(UTC).isoformat(),
            "dataset_sha256": dataset_sha256,
            "classification": "public-development-shadow",
            "holdout_accessed": False,
            "production_writes": False,
            "answer_judge_calls": 0,
            "selection_policy": {
                "comparison_decomposition": "explicit A-or-B comparisons only",
                "explicit_month_filter": False,
                "candidate_cutoffs": list(CANDIDATE_CUTOFFS),
                "selected_cutoff": selected["cutoff"] if selected else None,
            },
            "collection": collection,
            "qdrant_url": qdrant_url,
            "model_id": model_id,
            "dimensions": dimensions,
            "session_vectors": len(records),
            "query_vectors": query_vectors,
            "input_tokens_estimated": input_tokens,
            "pricing_assumption_usd_per_million": EMBEDDING_PRICE_USD_PER_MILLION,
            "estimated_provider_cost_usd": round(
                input_tokens / 1_000_000 * EMBEDDING_PRICE_USD_PER_MILLION, 8
            ),
            "indexing_latency_ms": round(indexing_latency_ms, 3),
            "retrieval_latency_ms": {
                "mean": round(sum(retrieval_latencies) / len(retrieval_latencies), 3),
                "max": round(max(retrieval_latencies), 3),
            },
            "cross_case_leakage_count": 0,
            "candidates": candidates,
            "selected": selected,
            "scored_results": scored,
            "cleanup": cleanup,
        }
    finally:
        cleanup["attempted"] = True
        try:
            if qdrant.collection_exists(collection):
                qdrant.delete_collection(collection_name=collection)
            cleanup["succeeded"] = not qdrant.collection_exists(collection)
        finally:
            qdrant.close()


def main() -> None:
    load_dotenv()
    parser = argparse.ArgumentParser()
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--qdrant-url", default="http://localhost:6333")
    parser.add_argument("--collection", default="")
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--approve-embedding-calls", action="store_true")
    args = parser.parse_args()
    artifact = run(args)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(artifact, indent=2), encoding="utf-8")
    print(
        json.dumps(
            {
                "output": str(args.output),
                "selected": artifact["selected"],
                "cleanup": artifact["cleanup"],
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
