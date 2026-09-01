from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from api.db.vector_store import QdrantService
from api.services.embedding_service import EmbeddingService
from api.services.retriever import MIN_SEMANTIC_SCORE, OVERFETCH_MULTIPLIER


def diagnose(
    *,
    question: str,
    tenant_id: str,
    proxy_user_id: str,
    qdrant_url: str,
    model_id: str,
    api_limit: int,
) -> dict[str, Any]:
    """Inspect raw vector candidates without invoking the API ranking path."""
    embedding_service = EmbeddingService()
    try:
        embedding = embedding_service.embed_sync(
            question,
            model_id=model_id,
            tenant_id=tenant_id,
        )
    finally:
        embedding_service.sync_http_client.close()

    qdrant = QdrantService(url=qdrant_url)
    raw_points = qdrant.search_memories(
        query_embedding=embedding.vector,
        tenant_id=tenant_id,
        proxy_user_id=proxy_user_id,
        limit=100,
        collection_name=embedding.qdrant_collection,
    )
    production_overfetch_limit = min(
        max(api_limit * OVERFETCH_MULTIPLIER, api_limit),
        50,
    )
    candidates: list[dict[str, Any]] = []
    for rank, point in enumerate(raw_points, start=1):
        payload = getattr(point, "payload", {}) or {}
        provenance = payload.get("provenance") or {}
        scope = provenance.get("scope") or {}
        score = float(getattr(point, "score", 0.0) or 0.0)
        candidates.append(
            {
                "rank": rank,
                "memory_id": str(payload.get("memory_id") or point.id),
                "content": payload.get("content"),
                "category": payload.get("category"),
                "importance_score": payload.get("importance_score"),
                "semantic_score": round(score, 9),
                "passes_semantic_floor": score >= MIN_SEMANTIC_SCORE,
                "inside_production_overfetch": rank <= production_overfetch_limit,
                "benchmark_session_number": scope.get("benchmark_session_number"),
                "benchmark_dialog_ids": scope.get("benchmark_dialog_ids") or [],
            }
        )
    return {
        "schema_version": "memoryos-locomo-raw-retrieval-diagnostic-v1",
        "question": question,
        "embedding_model_id": embedding.model_id,
        "qdrant_collection": embedding.qdrant_collection,
        "semantic_floor": MIN_SEMANTIC_SCORE,
        "api_limit": api_limit,
        "production_overfetch_limit": production_overfetch_limit,
        "raw_candidate_count": len(candidates),
        "candidates": candidates,
        "provider_calls": {"query_embedding": 1, "answer": 0, "judge": 0},
        "production_behavior_changed": False,
        "holdout_used": False,
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Inspect raw LoCoMo retrieval candidates"
    )
    parser.add_argument("--question", required=True)
    parser.add_argument("--tenant-id", required=True)
    parser.add_argument("--proxy-user-id", required=True)
    parser.add_argument("--qdrant-url", default="http://localhost:6333")
    parser.add_argument(
        "--model-id",
        default="openai-text-embedding-3-small-v1",
    )
    parser.add_argument("--api-limit", type=int, default=10)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = diagnose(
        question=args.question,
        tenant_id=args.tenant_id,
        proxy_user_id=args.proxy_user_id,
        qdrant_url=args.qdrant_url,
        model_id=args.model_id,
        api_limit=args.api_limit,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
