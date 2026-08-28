from __future__ import annotations

import argparse
import json
import math
import re
import time
from collections import Counter, defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from benchmarks.public.longmemeval.contract import load_dataset
from benchmarks.public.longmemeval.episodic_chunk_selection import (
    MAX_ACCEPTABLE_FILLER_PER_CASE,
    _select_cases,
    build_chunks,
)
from benchmarks.public.longmemeval.episodic_query_planner import plan_query
from benchmarks.public.longmemeval.episodic_shadow import ranking_metrics

BM25_K1 = 1.5
BM25_B = 0.75
RRF_K = 60
CANDIDATE_TOP_K = (3, 5, 10)
LEXICAL_SESSION_LIMIT = 10
STOPWORDS = {
    "a",
    "an",
    "and",
    "are",
    "as",
    "at",
    "be",
    "been",
    "but",
    "by",
    "did",
    "do",
    "does",
    "for",
    "from",
    "had",
    "has",
    "have",
    "how",
    "i",
    "in",
    "is",
    "it",
    "me",
    "my",
    "of",
    "on",
    "or",
    "that",
    "the",
    "to",
    "was",
    "were",
    "what",
    "when",
    "where",
    "which",
    "who",
    "with",
}


def tokenize(text: str) -> list[str]:
    return [
        token
        for token in re.findall(r"[a-z0-9]+", text.lower())
        if token not in STOPWORDS and len(token) > 1
    ]


def bm25_scores(query: str, documents: list[str]) -> list[float]:
    tokenized = [tokenize(document) for document in documents]
    query_tokens = set(tokenize(query))
    if not documents or not query_tokens:
        return [0.0] * len(documents)
    average_length = sum(len(tokens) for tokens in tokenized) / len(tokenized)
    document_frequency = Counter(
        token for tokens in tokenized for token in set(tokens) if token in query_tokens
    )
    scores: list[float] = []
    for tokens in tokenized:
        frequencies = Counter(tokens)
        score = 0.0
        for token in query_tokens:
            frequency = frequencies[token]
            if not frequency:
                continue
            inverse_document_frequency = math.log(
                1 + (len(documents) - document_frequency[token] + 0.5)
                / (document_frequency[token] + 0.5)
            )
            denominator = frequency + BM25_K1 * (
                1 - BM25_B + BM25_B * len(tokens) / max(average_length, 1)
            )
            score += inverse_document_frequency * (
                frequency * (BM25_K1 + 1) / denominator
            )
        scores.append(score)
    return scores


def reciprocal_rank_fusion(
    semantic_session_ids: list[str], lexical_session_ids: list[str]
) -> list[dict[str, float | int | str | None]]:
    scores: dict[str, float] = defaultdict(float)
    semantic_ranks = {
        session_id: rank for rank, session_id in enumerate(semantic_session_ids, 1)
    }
    lexical_ranks = {
        session_id: rank for rank, session_id in enumerate(lexical_session_ids, 1)
    }
    for ranks in (semantic_ranks, lexical_ranks):
        for session_id, rank in ranks.items():
            scores[session_id] += 1 / (RRF_K + rank)
    ordered = sorted(scores, key=lambda session_id: (-scores[session_id], session_id))
    return [
        {
            "session_id": session_id,
            "rrf_score": scores[session_id],
            "semantic_rank": semantic_ranks.get(session_id),
            "lexical_rank": lexical_ranks.get(session_id),
        }
        for session_id in ordered
    ]


def _aggregate(rows: list[dict[str, Any]], key: str) -> float:
    return sum(float(row["metrics"][key]) for row in rows) / len(rows)


def evaluate_top_k(cases: list[Any], fused: dict[str, list[dict[str, Any]]], top_k: int) -> dict[str, Any]:
    rows = []
    for case in cases:
        session_ids = [row["session_id"] for row in fused[case.question_id]][:top_k]
        rows.append(
            {
                "question_id": case.question_id,
                "question_type": case.question_type,
                "abstention": case.question_id.endswith("_abs"),
                "result_session_ids": session_ids,
                "metrics": ranking_metrics(session_ids, case.answer_session_ids),
            }
        )
    answerable = [row for row in rows if not row["abstention"]]
    abstentions = [row for row in rows if row["abstention"]]
    return {
        "top_k": top_k,
        "precision_at_k": _aggregate(rows, "precision_at_k"),
        "recall_at_k": _aggregate(rows, "recall_at_k"),
        "answerable_precision_at_k": _aggregate(answerable, "precision_at_k"),
        "answerable_recall_at_k": _aggregate(answerable, "recall_at_k"),
        "abstention_evidence_precision_at_k": _aggregate(
            abstentions, "precision_at_k"
        ),
        "abstention_evidence_recall_at_k": _aggregate(abstentions, "recall_at_k"),
        "mrr": _aggregate(rows, "mrr"),
        "ndcg": _aggregate(rows, "ndcg"),
        "irrelevant_filler_results": sum(
            int(row["metrics"]["irrelevant_filler_results"]) for row in rows
        ),
        "cases": rows,
    }


def run(args: argparse.Namespace) -> dict[str, Any]:
    all_cases, dataset_sha256 = load_dataset(args.dataset)
    cases, manifest = _select_cases(all_cases, dataset_sha256, args.manifest)
    semantic_artifact = json.loads(args.semantic_artifact.read_text(encoding="utf-8"))
    if semantic_artifact.get("dataset_sha256") != dataset_sha256:
        raise SystemExit("semantic artifact dataset hash mismatch")
    if semantic_artifact.get("sample_seed") != manifest.get("seed"):
        raise SystemExit("semantic artifact sample does not match the frozen manifest")

    started = time.perf_counter()
    chunks = build_chunks(cases, role_aware=True)
    chunks_by_question: dict[str, list[Any]] = defaultdict(list)
    for chunk in chunks:
        chunks_by_question[chunk.question_id].append(chunk)

    fused: dict[str, list[dict[str, Any]]] = {}
    lexical: dict[str, list[dict[str, Any]]] = {}
    for case in cases:
        case_chunks = chunks_by_question[case.question_id]
        documents = [chunk.text for chunk in case_chunks]
        variant_scores = [bm25_scores(variant, documents) for variant in plan_query(case.question)]
        scores = [max(values) for values in zip(*variant_scores, strict=True)]
        best_by_session: dict[str, dict[str, Any]] = {}
        for chunk, score in zip(case_chunks, scores, strict=True):
            if score <= 0:
                continue
            current = best_by_session.get(chunk.session_id)
            if current is None or score > current["score"]:
                best_by_session[chunk.session_id] = {
                    "session_id": chunk.session_id,
                    "score": score,
                    "chunk_index": chunk.chunk_index,
                    "turn_start": chunk.turn_start,
                    "turn_end": chunk.turn_end,
                    "roles": list(chunk.roles),
                    "content_hash": chunk.content_hash,
                }
        lexical_rows = sorted(
            best_by_session.values(),
            key=lambda row: (-row["score"], row["session_id"]),
        )[:LEXICAL_SESSION_LIMIT]
        lexical[case.question_id] = lexical_rows
        semantic_ids = [
            row["session_id"]
            for row in semantic_artifact["scored_results"][case.question_id]
        ]
        fused[case.question_id] = reciprocal_rank_fusion(
            semantic_ids,
            [row["session_id"] for row in lexical_rows],
        )

    candidates = [evaluate_top_k(cases, fused, top_k) for top_k in CANDIDATE_TOP_K]
    filler_limit = int(len(cases) * MAX_ACCEPTABLE_FILLER_PER_CASE)
    passing = [
        candidate
        for candidate in candidates
        if candidate["answerable_recall_at_k"] == 1.0
        and candidate["abstention_evidence_recall_at_k"] == 1.0
        and candidate["irrelevant_filler_results"] <= filler_limit
    ]
    selected = max(
        passing,
        key=lambda candidate: (
            candidate["precision_at_k"],
            candidate["ndcg"],
            -candidate["top_k"],
        ),
        default=None,
    )
    return {
        "schema_version": "longmemeval-episodic-hybrid-retrieval-v1",
        "created_at": datetime.now(UTC).isoformat(),
        "classification": "public-development-generalization",
        "dataset_sha256": dataset_sha256,
        "sample_seed": manifest["seed"],
        "sample_case_count": len(cases),
        "semantic_artifact": str(args.semantic_artifact),
        "holdout_accessed": False,
        "production_writes": False,
        "provider_calls": 0,
        "policy": {
            "bm25_k1": BM25_K1,
            "bm25_b": BM25_B,
            "rrf_k": RRF_K,
            "candidate_top_k": list(CANDIDATE_TOP_K),
            "lexical_session_limit": LEXICAL_SESSION_LIMIT,
            "filler_limit": filler_limit,
            "query_planner": {
                "coordinated_actions": True,
                "explicit_comparisons": True,
                "quantitative_conjunctions": True,
                "semantic_channel_reused_unchanged": True,
            },
        },
        "experiment_accepted": selected is not None,
        "experiment_rejection_reason": None
        if selected is not None
        else "No frozen Top-K candidate satisfied recall and filler criteria.",
        "selected": selected,
        "candidates": candidates,
        "lexical_results": lexical,
        "fused_results": fused,
        "latency_ms": round((time.perf_counter() - started) * 1000, 3),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--semantic-artifact", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    artifact = run(args)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(artifact, indent=2), encoding="utf-8")
    print(
        json.dumps(
            {
                "output": str(args.output),
                "selected": artifact["selected"],
                "provider_calls": 0,
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
