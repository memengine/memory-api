from __future__ import annotations

import argparse
import hashlib
import json
import math
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from dotenv import load_dotenv
from qdrant_client import QdrantClient, models

from api.services.embedding_service import EmbeddingService
from benchmarks.public.longmemeval.contract import (
    LongMemEvalCase,
    load_dataset,
    parse_longmemeval_datetime,
    select_stratified_smoke_cases,
)

FROZEN_QUESTION_IDS = {
    "58bf7951",
    "c4f10528",
    "54026fce",
    "gpt4_d31cdae3",
    "07741c45",
    "80ec1f4f_abs",
}
COLLECTION_PREFIX = "longmemeval_episodic_shadow_"
SEMANTIC_FLOOR = 0.315
EMBEDDING_PRICE_USD_PER_MILLION = 0.02


@dataclass(frozen=True, slots=True)
class EpisodicRecord:
    point_id: str
    question_id: str
    session_id: str
    occurred_at: str
    roles: tuple[str, ...]
    turn_count: int
    content_hash: str
    text: str


def render_session(case: LongMemEvalCase, index: int) -> str:
    turns = case.haystack_sessions[index]
    body = "\n".join(
        f"[{turn.role}]: {turn.content.strip()}"
        for turn in turns
        if turn.content.strip()
    )
    return f"Session date: {case.haystack_dates[index]}\n{body}"


def build_records(cases: list[LongMemEvalCase]) -> list[EpisodicRecord]:
    records: list[EpisodicRecord] = []
    for case in cases:
        for index, session_id in enumerate(case.haystack_session_ids):
            text = render_session(case, index)
            records.append(
                EpisodicRecord(
                    point_id=str(
                        uuid.uuid5(
                            uuid.NAMESPACE_URL,
                            f"memoryos:lme:episodic:{case.question_id}:{session_id}",
                        )
                    ),
                    question_id=case.question_id,
                    session_id=session_id,
                    occurred_at=parse_longmemeval_datetime(
                        case.haystack_dates[index]
                    ).isoformat(),
                    roles=tuple(sorted({turn.role for turn in case.haystack_sessions[index]})),
                    turn_count=len(case.haystack_sessions[index]),
                    content_hash=hashlib.sha256(text.encode()).hexdigest(),
                    text=text,
                )
            )
    return records


def ranking_metrics(
    ranked_session_ids: list[str], expected_session_ids: list[str]
) -> dict[str, float | int]:
    expected = set(expected_session_ids)
    hits = [session_id in expected for session_id in ranked_session_ids]
    hit_count = sum(hits)
    precision = hit_count / len(ranked_session_ids) if ranked_session_ids else 1.0
    recall = hit_count / len(expected) if expected else (1.0 if hit_count == 0 else 0.0)
    first = next((index + 1 for index, hit in enumerate(hits) if hit), None)
    dcg = sum((1.0 / math.log2(index + 2)) for index, hit in enumerate(hits) if hit)
    ideal_hits = min(len(expected), len(ranked_session_ids))
    idcg = sum(1.0 / math.log2(index + 2) for index in range(ideal_hits))
    return {
        "precision_at_k": precision,
        "recall_at_k": recall,
        "mrr": 1.0 / first if first else 0.0,
        "ndcg": dcg / idcg if idcg else (1.0 if not ranked_session_ids else 0.0),
        "relevant_results": hit_count,
        "irrelevant_filler_results": len(ranked_session_ids) - hit_count,
    }


def _token_count(text: str) -> int:
    try:
        import tiktoken

        return len(tiktoken.get_encoding("cl100k_base").encode(text))
    except (ImportError, RuntimeError):
        return max(1, len(text) // 4)


def _safe_local_qdrant_url(value: str) -> str:
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or parsed.hostname not in {
        "localhost",
        "127.0.0.1",
        "::1",
    }:
        raise SystemExit("episodic shadow requires a local disposable Qdrant endpoint")
    return value.rstrip("/")


def _aggregate(rows: list[dict[str, Any]], key: str) -> float:
    values = [float(row["episodic_metrics"][key]) for row in rows]
    return sum(values) / len(values)


def run(args: argparse.Namespace) -> dict[str, Any]:
    if not args.approve_embedding_calls:
        raise SystemExit("refusing provider calls without --approve-embedding-calls")
    cases, dataset_sha256 = load_dataset(args.dataset)
    cases = select_stratified_smoke_cases(cases)
    if {case.question_id for case in cases} != FROZEN_QUESTION_IDS:
        raise SystemExit("frozen six-case selection changed; refusing to run")
    collection = args.collection or f"{COLLECTION_PREFIX}{uuid.uuid4().hex[:12]}"
    if not collection.startswith(COLLECTION_PREFIX):
        raise SystemExit("collection must use the episodic shadow prefix")
    qdrant_url = _safe_local_qdrant_url(args.qdrant_url)
    records = build_records(cases)
    durable = json.loads(args.durable_artifact.read_text(encoding="utf-8"))
    durable_by_id = {row["question_id"]: row for row in durable["cases"]}
    qdrant = QdrantClient(url=qdrant_url, timeout=20)
    embedder = EmbeddingService()
    indexed = 0
    input_tokens = 0
    index_started = time.perf_counter()
    model_id = ""
    dimensions = 0
    rows: list[dict[str, Any]] = []
    cleanup = {"attempted": False, "succeeded": False}
    try:
        for record in records:
            embedded = embedder.embed_sync(record.text)
            model_id = embedded.model_id
            dimensions = embedded.dimensions
            input_tokens += _token_count(record.text)
            if indexed == 0:
                qdrant.create_collection(
                    collection_name=collection,
                    vectors_config=models.VectorParams(
                        size=dimensions, distance=models.Distance.COSINE
                    ),
                )
                for field in ("question_id", "session_id"):
                    qdrant.create_payload_index(
                        collection_name=collection,
                        field_name=field,
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
                            "record_type": "episodic_evidence",
                        },
                    )
                ],
                wait=True,
            )
            indexed += 1
        indexing_ms = (time.perf_counter() - index_started) * 1000

        for case in cases:
            started = time.perf_counter()
            query_embedding = embedder.embed_sync(case.question)
            input_tokens += _token_count(case.question)
            response = qdrant.query_points(
                collection_name=collection,
                query=query_embedding.vector,
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
            points = list(response.points)
            session_ids = [str(point.payload["session_id"]) for point in points]
            expected = [] if case.question_id.endswith("_abs") else case.answer_session_ids
            metrics = ranking_metrics(session_ids, expected)
            durable_ids = list(
                durable_by_id[case.question_id]["retrieval"]["retrieved_session_ids"]
            )
            fused_ids = list(dict.fromkeys([*session_ids, *durable_ids]))
            rows.append(
                {
                    "question_id": case.question_id,
                    "question_type": case.question_type,
                    "abstention": case.question_id.endswith("_abs"),
                    "expected_session_count": len(expected),
                    "episodic_results": [
                        {
                            "rank": rank,
                            "session_id": str(point.payload["session_id"]),
                            "score": float(point.score),
                            "occurred_at": point.payload["occurred_at"],
                            "roles": point.payload["roles"],
                            "turn_count": point.payload["turn_count"],
                            "content_hash": point.payload["content_hash"],
                        }
                        for rank, point in enumerate(points, 1)
                    ],
                    "episodic_metrics": metrics,
                    "durable_session_ids": durable_ids,
                    "durable_recall": durable_by_id[case.question_id]["retrieval"][
                        "evidence_session_recall"
                    ],
                    "fused_session_ids": fused_ids,
                    "fused_metrics": ranking_metrics(fused_ids, expected),
                    "retrieval_latency_ms": round(
                        (time.perf_counter() - started) * 1000, 3
                    ),
                }
            )
        return {
            "schema_version": "longmemeval-episodic-shadow-v1",
            "created_at": datetime.now(UTC).isoformat(),
            "dataset_sha256": dataset_sha256,
            "classification": "public-development-shadow",
            "holdout_accessed": False,
            "production_writes": False,
            "answer_judge_calls": 0,
            "collection": collection,
            "qdrant_url": qdrant_url,
            "model_id": model_id,
            "dimensions": dimensions,
            "session_vectors": indexed,
            "query_vectors": len(cases),
            "input_tokens_estimated": input_tokens,
            "pricing_assumption_usd_per_million": EMBEDDING_PRICE_USD_PER_MILLION,
            "estimated_provider_cost_usd": round(
                input_tokens / 1_000_000 * EMBEDDING_PRICE_USD_PER_MILLION, 8
            ),
            "indexing_latency_ms": round(indexing_ms, 3),
            "summary": {
                key: _aggregate(rows, key)
                for key in ("precision_at_k", "recall_at_k", "mrr", "ndcg")
            },
            "cases": rows,
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
    parser.add_argument("--durable-artifact", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--qdrant-url", default="http://localhost:6333")
    parser.add_argument("--collection", default="")
    parser.add_argument("--limit", type=int, default=10)
    parser.add_argument("--approve-embedding-calls", action="store_true")
    args = parser.parse_args()
    artifact = run(args)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(artifact, indent=2), encoding="utf-8")
    print(json.dumps({"output": str(args.output), "summary": artifact["summary"]}, indent=2))


if __name__ == "__main__":
    main()
