from __future__ import annotations

import argparse
import hashlib
import json
import math
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from qdrant_client import QdrantClient, models

from api.services.embedding_service import EmbeddingResult, EmbeddingService
from benchmarks.public.longmemeval.contract import (
    LongMemEvalCase,
    load_dataset,
    parse_longmemeval_datetime,
    select_stratified_smoke_cases,
)
from benchmarks.public.longmemeval.episodic_selection import (
    CANDIDATE_CUTOFFS,
    _evaluate_candidate,
    decompose_comparison_query,
)
from benchmarks.public.longmemeval.episodic_shadow import (
    COLLECTION_PREFIX,
    EMBEDDING_PRICE_USD_PER_MILLION,
    FROZEN_QUESTION_IDS,
    SEMANTIC_FLOOR,
    _safe_local_qdrant_url,
    _token_count,
)

MAX_CHUNK_CHARS = 2400
OVERSIZED_TURN_OVERLAP_CHARS = 240
MAX_ACCEPTABLE_FILLER_PER_CASE = 1 / 3


def _select_cases(
    all_cases: list[LongMemEvalCase],
    dataset_sha256: str,
    manifest_path: Path | None,
) -> tuple[list[LongMemEvalCase], dict[str, Any] | None]:
    if manifest_path is None:
        cases = select_stratified_smoke_cases(all_cases)
        if {case.question_id for case in cases} != FROZEN_QUESTION_IDS:
            raise SystemExit("frozen six-case selection changed; refusing to run")
        return cases, None

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("classification") != "public-development-generalization":
        raise SystemExit("manifest is not an approved public-development sample")
    if manifest.get("dataset_sha256") != dataset_sha256:
        raise SystemExit("manifest dataset hash does not match the loaded dataset")
    question_ids = list(manifest.get("question_ids") or [])
    if len(question_ids) != 30 or len(set(question_ids)) != 30:
        raise SystemExit("generalization manifest must contain 30 unique cases")
    if set(question_ids) & FROZEN_QUESTION_IDS:
        raise SystemExit("generalization manifest overlaps the six-case pilot")
    by_id = {case.question_id: case for case in all_cases}
    if any(question_id not in by_id for question_id in question_ids):
        raise SystemExit("generalization manifest contains an unknown question ID")
    return [by_id[question_id] for question_id in question_ids], manifest


@dataclass(frozen=True, slots=True)
class EpisodicChunk:
    point_id: str
    question_id: str
    session_id: str
    chunk_index: int
    turn_start: int
    turn_end: int
    occurred_at: str
    roles: tuple[str, ...]
    content_hash: str
    text: str


@dataclass(frozen=True, slots=True)
class _TurnPiece:
    turn_index: int
    role: str
    text: str


def _split_turn(turn_index: int, role: str, content: str) -> list[_TurnPiece]:
    rendered = f"[{role}]: {content.strip()}"
    if len(rendered) <= MAX_CHUNK_CHARS:
        return [_TurnPiece(turn_index, role, rendered)]
    pieces: list[_TurnPiece] = []
    start = 0
    while start < len(rendered):
        end = min(start + MAX_CHUNK_CHARS, len(rendered))
        if end < len(rendered):
            boundary = rendered.rfind(" ", start + MAX_CHUNK_CHARS // 2, end)
            if boundary > start:
                end = boundary
        pieces.append(_TurnPiece(turn_index, role, rendered[start:end].strip()))
        if end == len(rendered):
            break
        start = max(end - OVERSIZED_TURN_OVERLAP_CHARS, start + 1)
    return pieces


def build_chunks(
    cases: list[LongMemEvalCase], *, role_aware: bool = False
) -> list[EpisodicChunk]:
    chunks: list[EpisodicChunk] = []
    for case in cases:
        for session_index, session_id in enumerate(case.haystack_session_ids):
            pieces: list[_TurnPiece] = []
            for turn_index, turn in enumerate(case.haystack_sessions[session_index]):
                if turn.content.strip():
                    pieces.extend(_split_turn(turn_index, turn.role, turn.content))

            groups: list[list[_TurnPiece]] = []
            current: list[_TurnPiece] = []
            current_size = 0
            for piece in pieces:
                added_size = len(piece.text) + (1 if current else 0)
                crosses_role_boundary = bool(
                    role_aware and current and current[-1].role != piece.role
                )
                if current and (
                    current_size + added_size > MAX_CHUNK_CHARS
                    or crosses_role_boundary
                ):
                    groups.append(current)
                    current = []
                    current_size = 0
                current.append(piece)
                current_size += len(piece.text) + (1 if current_size else 0)
            if current:
                groups.append(current)

            occurred_at = parse_longmemeval_datetime(
                case.haystack_dates[session_index]
            ).isoformat()
            for chunk_index, group in enumerate(groups):
                body = "\n".join(piece.text for piece in group)
                text = f"Session date: {case.haystack_dates[session_index]}\n{body}"
                chunks.append(
                    EpisodicChunk(
                        point_id=str(
                            uuid.uuid5(
                                uuid.NAMESPACE_URL,
                                "memoryos:lme:episodic-chunk:"
                                f"{case.question_id}:{session_id}:{chunk_index}",
                            )
                        ),
                        question_id=case.question_id,
                        session_id=session_id,
                        chunk_index=chunk_index,
                        turn_start=min(piece.turn_index for piece in group),
                        turn_end=max(piece.turn_index for piece in group),
                        occurred_at=occurred_at,
                        roles=tuple(sorted({piece.role for piece in group})),
                        content_hash=hashlib.sha256(text.encode()).hexdigest(),
                        text=text,
                    )
                )
    return chunks


def _embed_chunk_texts(
    embedder: EmbeddingService,
    chunks: list[EpisodicChunk],
    workers: int,
    openai_batch_size: int = 0,
) -> Any:
    if openai_batch_size:
        if openai_batch_size < 2 or openai_batch_size > 128:
            raise SystemExit("OpenAI batch size must be between 2 and 128")
        model = embedder.get_active_model_sync()
        if model.provider != "openai":
            raise SystemExit("benchmark batch transport requires the active OpenAI model")
        provider = embedder.llm_router.get_provider(
            "openai",
            embed_model=model.model_name,
            embedding_dimensions=model.dimensions,
        )
        for start in range(0, len(chunks), openai_batch_size):
            batch = chunks[start : start + openai_batch_size]
            response = provider.http_client.post(
                "https://api.openai.com/v1/embeddings",
                headers={
                    "Authorization": f"Bearer {provider.api_key or ''}",
                    "content-type": "application/json",
                },
                json={
                    "model": model.model_name,
                    "input": [chunk.text for chunk in batch],
                    "dimensions": model.dimensions,
                },
                timeout=30.0,
            )
            response.raise_for_status()
            data = sorted(response.json().get("data") or [], key=lambda row: row["index"])
            if len(data) != len(batch):
                raise RuntimeError("OpenAI batch embedding response length mismatch")
            for row in data:
                yield EmbeddingResult(
                    vector=[float(value) for value in row["embedding"]],
                    model_id=model.id,
                    dimensions=model.dimensions,
                    qdrant_collection=model.qdrant_collection,
                )
        return
    if workers < 1 or workers > 8:
        raise SystemExit("embedding workers must be between 1 and 8")
    if workers == 1:
        for chunk in chunks:
            yield embedder.embed_sync(chunk.text)
        return
    with ThreadPoolExecutor(max_workers=workers) as executor:
        yield from executor.map(embedder.embed_sync, (chunk.text for chunk in chunks))


def run(args: argparse.Namespace) -> dict[str, Any]:
    if not args.approve_embedding_calls:
        raise SystemExit("refusing provider calls without --approve-embedding-calls")
    all_cases, dataset_sha256 = load_dataset(args.dataset)
    cases, manifest = _select_cases(all_cases, dataset_sha256, args.manifest)
    qdrant_url = _safe_local_qdrant_url(args.qdrant_url)
    collection = args.collection or f"{COLLECTION_PREFIX}chunks_{uuid.uuid4().hex[:10]}"
    if not collection.startswith(f"{COLLECTION_PREFIX}chunks_"):
        raise SystemExit("chunk collection must use the disposable shadow prefix")

    chunks = build_chunks(cases, role_aware=args.role_aware)
    chunk_by_id = {chunk.point_id: chunk for chunk in chunks}
    qdrant = QdrantClient(url=qdrant_url, timeout=20)
    embedder = EmbeddingService()
    input_tokens = 0
    query_vectors = 0
    model_id = ""
    dimensions = 0
    cleanup = {"attempted": False, "succeeded": False}
    scored: dict[str, list[dict[str, Any]]] = {}
    retrieval_latencies: list[float] = []
    index_started = time.perf_counter()
    try:
        embedded_chunks = _embed_chunk_texts(
            embedder,
            chunks,
            args.embedding_workers,
            args.openai_batch_size,
        )
        for index, (chunk, embedded) in enumerate(zip(chunks, embedded_chunks, strict=True)):
            input_tokens += _token_count(chunk.text)
            model_id = embedded.model_id
            dimensions = embedded.dimensions
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
                        id=chunk.point_id,
                        vector=embedded.vector,
                        payload={
                            "question_id": chunk.question_id,
                            "session_id": chunk.session_id,
                            "chunk_index": chunk.chunk_index,
                            "turn_start": chunk.turn_start,
                            "turn_end": chunk.turn_end,
                            "occurred_at": chunk.occurred_at,
                            "roles": list(chunk.roles),
                            "content_hash": chunk.content_hash,
                        },
                    )
                ],
                wait=True,
            )
        indexing_latency_ms = (time.perf_counter() - index_started) * 1000

        for case in cases:
            started = time.perf_counter()
            best_sessions: dict[str, dict[str, Any]] = {}
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
                    limit=args.chunk_limit,
                    score_threshold=SEMANTIC_FLOOR,
                    with_payload=True,
                    with_vectors=False,
                )
                for point in response.points:
                    point_id = str(point.id)
                    chunk = chunk_by_id[point_id]
                    session_id = chunk.session_id
                    score = float(point.score)
                    current = best_sessions.get(session_id)
                    if current is None or score > current["score"]:
                        best_sessions[session_id] = {
                            "session_id": session_id,
                            "score": score,
                            "matched_query_index": variant_index,
                            "chunk_index": chunk.chunk_index,
                            "turn_start": chunk.turn_start,
                            "turn_end": chunk.turn_end,
                            "occurred_at": chunk.occurred_at,
                            "roles": list(chunk.roles),
                            "content_hash": chunk.content_hash,
                        }
            scored[case.question_id] = sorted(
                best_sessions.values(),
                key=lambda row: (-row["score"], row["session_id"]),
            )[: args.session_limit]
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
            and candidate["irrelevant_filler_results"]
            <= int(len(cases) * MAX_ACCEPTABLE_FILLER_PER_CASE)
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
            "schema_version": "longmemeval-episodic-chunk-selection-v2"
            if args.role_aware
            else "longmemeval-episodic-chunk-selection-v1",
            "created_at": datetime.now(UTC).isoformat(),
            "dataset_sha256": dataset_sha256,
            "sample_manifest": str(args.manifest) if args.manifest else None,
            "sample_seed": manifest.get("seed") if manifest else None,
            "sample_case_count": len(cases),
            "classification": "public-development-shadow",
            "holdout_accessed": False,
            "production_writes": False,
            "answer_judge_calls": 0,
            "experiment_accepted": selected is not None,
            "experiment_rejection_reason": None
            if selected is not None
            else (
                "No cutoff preserves 100% answerable and abstention-evidence recall "
                "while satisfying the frozen filler-per-case limit."
            ),
            "chunk_policy": {
                "max_chunk_chars": MAX_CHUNK_CHARS,
                "oversized_turn_overlap_chars": OVERSIZED_TURN_OVERLAP_CHARS,
                "grouping": "same-role adjacent turns"
                if args.role_aware
                else "adjacent role-labelled turns",
                "role_aware": args.role_aware,
                "session_aggregation": "maximum chunk similarity",
            },
            "selection_policy": {
                "comparison_decomposition": "explicit A-or-B comparisons only",
                "explicit_month_filter": False,
                "candidate_cutoffs": list(CANDIDATE_CUTOFFS),
                "max_acceptable_filler_per_case": MAX_ACCEPTABLE_FILLER_PER_CASE,
                "max_acceptable_filler_results": int(
                    len(cases) * MAX_ACCEPTABLE_FILLER_PER_CASE
                ),
                "selected_cutoff": selected["cutoff"] if selected else None,
            },
            "collection": collection,
            "qdrant_url": qdrant_url,
            "model_id": model_id,
            "dimensions": dimensions,
            "chunk_vectors": len(chunks),
            "source_sessions": len({(c.question_id, c.session_id) for c in chunks}),
            "query_vectors": query_vectors,
            "embedding_workers": args.embedding_workers,
            "openai_batch_size": args.openai_batch_size,
            "chunk_embedding_provider_requests": math.ceil(
                len(chunks) / args.openai_batch_size
            )
            if args.openai_batch_size
            else len(chunks),
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
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--qdrant-url", default="http://localhost:6333")
    parser.add_argument("--collection", default="")
    parser.add_argument("--chunk-limit", type=int, default=40)
    parser.add_argument("--session-limit", type=int, default=10)
    parser.add_argument("--role-aware", action="store_true")
    parser.add_argument("--embedding-workers", type=int, default=1)
    parser.add_argument("--openai-batch-size", type=int, default=0)
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
