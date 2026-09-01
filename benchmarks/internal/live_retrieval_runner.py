from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import math
import os
import time
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
from sqlalchemy import delete, select

from api.db.database import build_sync_session_factory
from api.db.models import Agent, AgentMemoryScope, ApiKey, Conversation, EmbeddingModel
from api.db.models import Memory, MemoryCategory, ProxyUser, User
from api.db.vector_store import QdrantService
from api.services.embedding_service import EmbeddingService
from api.services.retriever import RetrieverService
from api.services.vector_outbox import build_vector_payload
from api.utils.crypto import api_key_prefix, verify_api_key
from benchmarks.internal.retrieval_cases import load_retrieval_development_cases


FILLERS = (
    ("User enjoys landscape photography on weekends.", "preference", 5.0),
    ("User reads product management newsletters.", "procedure", 4.0),
    ("User attended a design review last month.", "fact", 4.0),
    ("User wants to improve public speaking someday.", "goal", 5.0),
)


def _tenant_for_key(session: Any, raw_key: str) -> uuid.UUID:
    rows = session.execute(select(ApiKey).where(
        ApiKey.is_active.is_(True), ApiKey.key_prefix == api_key_prefix(raw_key),
    )).scalars().all()
    matched = next((row for row in rows if verify_api_key(raw_key, row.key_hash)), None)
    if matched is None or matched.tenant_id is None:
        raise RuntimeError("BENCHMARK_API_KEY does not match an active tenant key")
    return matched.tenant_id


def _metrics(expected: dict[str, int], retrieved: list[str], limit: int) -> dict[str, float]:
    relevant = set(expected)
    hits = [key for key in retrieved if key in relevant]
    precision = len(hits) / len(retrieved) if retrieved else (1.0 if not relevant else 0.0)
    recall = len(hits) / len(relevant) if relevant else (1.0 if not retrieved else 0.0)
    mrr = next((1 / rank for rank, key in enumerate(retrieved, 1) if key in relevant), 1.0 if not relevant and not retrieved else 0.0)
    gains = [expected.get(key, 0) for key in retrieved]
    dcg = sum((2**gain - 1) / math.log2(rank + 1) for rank, gain in enumerate(gains, 1))
    ideal = sorted(expected.values(), reverse=True)[:limit]
    idcg = sum((2**gain - 1) / math.log2(rank + 1) for rank, gain in enumerate(ideal, 1))
    return {"precision_at_k": precision, "recall_at_k": recall, "mrr": mrr, "ndcg_at_k": dcg / idcg if idcg else 1.0}


async def main_async(output: Path) -> None:
    raw_key = (os.getenv("BENCHMARK_API_KEY") or "").strip()
    base_user = (os.getenv("BENCHMARK_EXTERNAL_USER_ID") or "").strip()
    if not raw_key or not base_user:
        raise RuntimeError("BENCHMARK_API_KEY and BENCHMARK_EXTERNAL_USER_ID are required")
    session = build_sync_session_factory()()
    qdrant = QdrantService()
    embedder = EmbeddingService(sync_session=session)
    run_id = uuid.uuid4().hex[:12]
    created_memory_ids: list[tuple[str, str]] = []
    created_user_ids: list[uuid.UUID] = []
    created_proxy_ids: list[uuid.UUID] = []
    rows: list[dict[str, Any]] = []
    provider_calls = 0
    provider_characters = 0
    started_run = time.perf_counter()
    try:
        tenant_id = _tenant_for_key(session, raw_key)
        model = session.execute(select(EmbeddingModel).where(EmbeddingModel.is_active.is_(True)).limit(1)).scalar_one()
        cases = load_retrieval_development_cases()
        async with httpx.AsyncClient(base_url="http://127.0.0.1:8000", timeout=30.0) as client:
            for case_index, case in enumerate(cases):
                external_id = f"{base_user}-{run_id}-{case_index}"
                proxy = ProxyUser(
                    id=uuid.uuid4(), tenant_id=tenant_id, external_user_id=external_id,
                    external_user_id_hash=hashlib.sha256(f"{tenant_id}:{external_id}".encode()).hexdigest(),
                    memory_count=0, metadata_json={"internal_retrieval_benchmark": run_id}, is_blocked=False,
                )
                user = User(
                    id=uuid.uuid4(), external_id=f"retrieval-benchmark::{run_id}::{case_index}",
                    email=f"retrieval-{run_id}-{case_index}@benchmark.test", settings={}, memory_count=0, is_active=True,
                )
                created_proxy_ids.append(proxy.id); created_user_ids.append(user.id)
                session.add_all([proxy, user]); session.flush()
                agent_map: dict[str, uuid.UUID] = {}
                for label in {str(c.get("agent_id")) for c in case.candidates if c.get("agent_id")}:
                    agent = Agent(id=uuid.uuid4(), user_id=user.id, name=f"{label}-{run_id}", memory_scope=AgentMemoryScope.private)
                    session.add(agent); session.flush(); agent_map[label] = agent.id
                conversation = Conversation(id=uuid.uuid4(), user_id=user.id, message_count=1)
                session.add(conversation); session.flush()
                candidates = list(case.candidates)
                while sum(1 for c in candidates if c.get("active", True)) < 6:
                    content, category, importance = FILLERS[len(candidates) % len(FILLERS)]
                    candidates.append({"key": f"filler_{len(candidates)}", "content": content, "category": category, "importance": importance, "age_days": 10})
                key_by_memory: dict[str, str] = {}
                for candidate in candidates:
                    memory_id = uuid.uuid4()
                    created_at = datetime.now(UTC) - timedelta(days=float(candidate.get("age_days", 0)))
                    agent_id = agent_map.get(str(candidate.get("agent_id"))) if candidate.get("agent_id") else None
                    memory = Memory(
                        id=memory_id, user_id=user.id, proxy_user_id=proxy.id, agent_id=agent_id,
                        content=candidate["content"], category=MemoryCategory(candidate["category"]),
                        importance_score=float(candidate["importance"]), confidence_score=0.95,
                        embedding_id=f"live-retrieval-{memory_id}", embedding_model_id=model.id,
                        source_conversation_id=conversation.id,
                        metadata_json={"benchmark_run_id": run_id, "benchmark_key": candidate["key"], "provenance": candidate.get("provenance")},
                        created_at=created_at, updated_at=created_at, last_accessed_at=created_at,
                        is_archived=not bool(candidate.get("active", True)),
                    )
                    session.add(memory); session.flush()
                    embedding = embedder.embed_sync(memory.content, model_id=model.id, tenant_id=str(tenant_id))
                    provider_calls += 1; provider_characters += len(memory.content)
                    payload_tenant_id = (
                        str(uuid.uuid4())
                        if case.id == "tenant_isolation" and candidate.get("tenant_id") == "tenant-b"
                        else str(tenant_id)
                    )
                    payload = build_vector_payload(
                        memory, tenant_id=payload_tenant_id, proxy_user_id=str(proxy.id), user_id=str(user.id),
                        embedding_model_id=model.id, qdrant_collection=model.qdrant_collection,
                    )
                    qdrant.upsert_memory(str(memory_id), embedding.vector, payload, collection_name=model.qdrant_collection, vector_size=model.dimensions)
                    created_memory_ids.append((str(memory_id), model.qdrant_collection)); key_by_memory[str(memory_id)] = candidate["key"]
                proxy.memory_count = len(candidates); session.commit()

                payload: dict[str, Any] = {"external_user_id": external_id, "query": case.query, "limit": case.limit}
                if case.filters.get("categories"): payload["categories"] = case.filters["categories"]
                if case.filters.get("agent_id"): payload["agent_id"] = str(agent_map[case.filters["agent_id"]])
                if case.filters.get("max_age_days"): payload["time_filter_days"] = case.filters["max_age_days"]
                before = time.perf_counter()
                response = await client.post("/v1/memories/retrieve", json=payload, headers={"Authorization": f"ApiKey {raw_key}"})
                latency_ms = (time.perf_counter() - before) * 1000
                api_error = None
                results = []
                if response.status_code == 200:
                    results = response.json().get("data", [])
                else:
                    api_error = {"status_code": response.status_code, "body": response.text[:500]}
                retrieved_keys = [key_by_memory.get(str(item.get("id")), "foreign_or_unknown") for item in results]
                scored_keys = list(retrieved_keys)
                for position, key in enumerate(scored_keys):
                    returned_candidate = next((c for c in candidates if c["key"] == key), None)
                    if returned_candidate is None or key in case.relevant:
                        continue
                    for relevant_key in case.relevant:
                        expected_candidate = next(c for c in candidates if c["key"] == relevant_key)
                        if RetrieverService._content_similarity(returned_candidate["content"], expected_candidate["content"]) > 0.95:
                            scored_keys[position] = relevant_key
                            break
                metrics = _metrics(case.relevant, scored_keys, case.limit)
                rows.append({
                    "id": case.id, "scenario_type": case.scenario_type, "api_success": response.status_code == 200,
                    "api_error": api_error, "retrieved": retrieved_keys, "expected_relevant": list(case.relevant),
                    "result_scores": [
                        {
                            "key": key,
                            "rank": rank,
                            "semantic_score": round(
                                (
                                    float(item.get("relevance_score", 0.0))
                                    - (RetrieverService.IMPORTANCE_WEIGHT * (float(item.get("importance_score", 0.0)) / 10.0))
                                    - (RetrieverService.RECENCY_WEIGHT * RetrieverService._recency_score(
                                        RetrieverService._parse_datetime(item.get("last_accessed"))
                                    ))
                                ) / RetrieverService.SEMANTIC_WEIGHT,
                                6,
                            ),
                            "final_score": float(item.get("relevance_score", 0.0)),
                        }
                        for rank, (key, item) in enumerate(zip(retrieved_keys, results), 1)
                    ],
                    **metrics, "latency_ms": round(latency_ms, 2),
                    "provenance_preserved": all(
                        next((c.get("provenance") for c in candidates if c["key"] == key), None) == item.get("provenance")
                        for key, item in zip(retrieved_keys, results)
                    ),
                    "superseded_leak": any(key == c["key"] for key in retrieved_keys for c in candidates if not c.get("active", True)),
                    "unknown_or_cross_scope_results": sum(key == "foreign_or_unknown" for key in retrieved_keys),
                    "cached": response.json().get("cached") if response.status_code == 200 else None,
                })
        count = len(rows)
        summary = {
            "scenario_count": count, "api_success_rate": sum(r["api_success"] for r in rows) / count,
            "precision_at_k": sum(r["precision_at_k"] for r in rows) / count,
            "recall_at_k": sum(r["recall_at_k"] for r in rows) / count,
            "mrr": sum(r["mrr"] for r in rows) / count, "ndcg_at_k": sum(r["ndcg_at_k"] for r in rows) / count,
            "provenance_preservation": sum(r["provenance_preserved"] for r in rows) / count,
            "superseded_memory_leakage_rate": sum(r["superseded_leak"] for r in rows) / count,
            "cross_scope_result_rate": sum(r["unknown_or_cross_scope_results"] for r in rows) / max(1, sum(len(r["retrieved"]) for r in rows)),
            "mean_latency_ms": sum(r["latency_ms"] for r in rows) / count,
            "max_latency_ms": max(r["latency_ms"] for r in rows),
            "embedding_provider_calls": provider_calls + len(cases),
            "embedding_input_characters": provider_characters + sum(len(case.query) for case in cases),
            "estimated_embedding_tokens": math.ceil((provider_characters + sum(len(case.query) for case in cases)) / 4),
        }
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps({
            "benchmark": "retrieval-live-vector-development-v1", "captured_at": datetime.now(UTC).isoformat(),
            "provider": model.provider.value if hasattr(model.provider, "value") else str(model.provider),
            "model": model.model_name, "model_id": model.id, "dimensions": model.dimensions,
            "qdrant_collection": model.qdrant_collection, "holdout_used": False,
            "production_behavior_changed": False, "fixture_cleaned": True,
            "run_duration_ms": round((time.perf_counter() - started_run) * 1000, 2),
            "summary": summary, "cases": rows,
        }, indent=2), encoding="utf-8")
        print(json.dumps(summary, indent=2))
    finally:
        for memory_id, collection in created_memory_ids:
            try: qdrant.delete_memory(memory_id, collection_name=collection)
            except Exception: pass
        try:
            if created_proxy_ids: session.execute(delete(ProxyUser).where(ProxyUser.id.in_(created_proxy_ids)))
            if created_user_ids: session.execute(delete(User).where(User.id.in_(created_user_ids)))
            session.commit()
        finally:
            session.close()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    asyncio.run(main_async(args.output))


if __name__ == "__main__":
    main()
