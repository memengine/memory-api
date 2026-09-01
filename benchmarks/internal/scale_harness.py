from __future__ import annotations

import argparse
import json
import os
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import delete, func, select

from api.db.database import build_sync_session_factory
from api.db.models import (
    Agent, AgentMemoryScope, ApiKey, EmbeddingModel, EmbeddingProvider, ExtractionJob, Memory, MemoryClaim,
    MemoryClaimRevision, MemorySourceEvent, PlanTier, ProxyUser, ServiceWriter,
    Tenant, TenantBudget, User, VectorSyncOutbox, VectorSyncStatus,
)
from api.config.plan_limits import apply_plan_limits
from api.db.vector_store import QdrantService
from api.infra.benchmark_provider import benchmark_provider_enabled
from api.services.webhook_event_service import generate_webhook_secret
from api.tasks.extraction_tasks import _redis_client
from api.settings import get_settings
from api.utils.crypto import api_key_prefix, hash_api_key


USER_PREFIX = "scale_{run_id}_"


def require_safe_environment() -> None:
    if get_settings().app_env.strip().lower() in {"production", "prod"}:
        raise RuntimeError("Scale benchmark is disabled in production.")
    if os.getenv("MEMORYOS_SCALE_DEDICATED") != "1":
        raise RuntimeError("Set MEMORYOS_SCALE_DEDICATED=1 only inside a disposable dedicated stack.")
    if not os.getenv("BENCHMARK_API_KEY"):
        raise RuntimeError("BENCHMARK_API_KEY is required.")
    if not os.getenv("SCALE_SOURCE_SERVICE"):
        raise RuntimeError("SCALE_SOURCE_SERVICE must name a writer bound to the benchmark key.")
    if os.getenv("SCALE_COMPOSE_PROJECT") != "memoryos-scale":
        raise RuntimeError("SCALE_COMPOSE_PROJECT must be memoryos-scale.")
    database_url = os.getenv("DATABASE_URL", "")
    redis_url = os.getenv("REDIS_URL", "")
    qdrant_url = os.getenv("QDRANT_URL", "")
    if "/memoryos_scale" not in database_url:
        raise RuntimeError("Scale benchmark requires the isolated memoryos_scale database.")
    if "redis://redis:6379/0" not in redis_url:
        raise RuntimeError("Scale benchmark requires its isolated Compose Redis instance.")
    if qdrant_url.rstrip("/") != "http://qdrant:6333":
        raise RuntimeError("Scale benchmark requires its isolated Compose Qdrant instance.")
    if os.getenv("QDRANT_COLLECTION") != "scale_memories_v1":
        raise RuntimeError("Scale benchmark requires the isolated Qdrant collection.")
    for name in ("OPENAI_API_KEY", "GEMINI_API_KEY", "ANTHROPIC_API_KEY"):
        if os.getenv(name, "").strip():
            raise RuntimeError(f"{name} must be empty in zero-cost scale mode.")
    if not benchmark_provider_enabled():
        raise RuntimeError("Deterministic benchmark provider is not active.")
    cache_mode = os.getenv("BENCHMARK_CACHE_INVALIDATION_MODE", "legacy-scan")
    cache_namespace = os.getenv("BENCHMARK_CACHE_NAMESPACE", "v1")
    if cache_mode not in {"legacy-scan", "generation-v1"}:
        raise RuntimeError("Scale benchmark cache invalidation mode is not recognized.")
    if cache_mode == "generation-v1" and cache_namespace != "v2":
        raise RuntimeError("Generation cache invalidation requires the isolated v2 cache namespace.")
    if os.getenv("BENCHMARK_REDIS_TCP_PREFLIGHT", "enabled") not in {"enabled", "disabled"}:
        raise RuntimeError("Scale benchmark Redis TCP preflight mode is not recognized.")


def bootstrap() -> dict[str, Any]:
    require_safe_environment()
    raw_key = os.environ["BENCHMARK_API_KEY"]
    service = os.environ["SCALE_SOURCE_SERVICE"]
    factory = build_sync_session_factory()
    session = factory()
    try:
        model = session.get(EmbeddingModel, "benchmark-hash-v1")
        if model is None:
            session.add(EmbeddingModel(
                id="benchmark-hash-v1",
                provider=EmbeddingProvider.openai,
                model_name="benchmark-hash-v1",
                dimensions=1536,
                qdrant_collection="scale_memories_v1",
                is_active=False,
            ))
            session.flush()
        existing = session.execute(select(Tenant).where(Tenant.company_name == "MemoryOS Scale Benchmark")).scalar_one_or_none()
        if existing is not None:
            agents = session.execute(select(Agent).join(User).where(User.external_id == "scale-benchmark-owner").order_by(Agent.name)).scalars().all()
            session.commit()
            return {"tenant_id": str(existing.id), "agent_ids": [str(agent.id) for agent in agents], "created": False}
        tenant = Tenant(company_name="MemoryOS Scale Benchmark", plan_tier=PlanTier.scale, is_active=True, metadata_json={"benchmark_only": True})
        session.add(tenant); session.flush()
        session.add(TenantBudget(tenant_id=tenant.id, plan_tier=PlanTier.scale, webhook_secret=generate_webhook_secret()))
        apply_plan_limits(str(tenant.id), PlanTier.scale.value, session)
        key = ApiKey(tenant_id=tenant.id, user_id=None, key_hash=hash_api_key(raw_key), key_prefix=api_key_prefix(raw_key), name="Disposable scale benchmark", permissions=["read", "write"], rate_limit_per_minute=10000, is_active=True)
        session.add(key); session.flush()
        session.add(ServiceWriter(tenant_id=tenant.id, api_key_id=key.id, service_key=service, display_name="Scale benchmark fixture", authority_rules={}))
        owner = User(external_id="scale-benchmark-owner", email="scale-benchmark@invalid.local", settings={"benchmark_only": True})
        session.add(owner); session.flush()
        agents = [
            Agent(user_id=owner.id, name="Scale Agent A", description="Disposable benchmark agent", memory_scope=AgentMemoryScope.private),
            Agent(user_id=owner.id, name="Scale Agent B", description="Disposable benchmark agent", memory_scope=AgentMemoryScope.private),
        ]
        session.add_all(agents); session.commit()
        return {"tenant_id": str(tenant.id), "agent_ids": [str(agent.id) for agent in agents], "created": True}
    finally:
        session.close()


def _scope(run_id: str, session: Any) -> dict[str, Any]:
    prefix = USER_PREFIX.format(run_id=run_id) + "%"
    proxies = session.execute(select(ProxyUser).where(ProxyUser.external_user_id.like(prefix))).scalars().all()
    proxy_ids = [row.id for row in proxies]
    memories = [] if not proxy_ids else session.execute(select(Memory).where(Memory.proxy_user_id.in_(proxy_ids))).scalars().all()
    memory_ids = [row.id for row in memories]
    claims = [] if not proxy_ids else session.execute(select(MemoryClaim).where(MemoryClaim.proxy_user_id.in_(proxy_ids))).scalars().all()
    claim_ids = [row.id for row in claims]
    revisions = [] if not claim_ids else session.execute(select(MemoryClaimRevision).where(MemoryClaimRevision.claim_id.in_(claim_ids))).scalars().all()
    jobs = [] if not proxy_ids else session.execute(select(ExtractionJob).where(ExtractionJob.proxy_user_id.in_(proxy_ids))).scalars().all()
    events = [] if not proxy_ids else session.execute(select(MemorySourceEvent).where(MemorySourceEvent.proxy_user_id.in_(proxy_ids))).scalars().all()
    outbox = [] if not memory_ids else session.execute(select(VectorSyncOutbox).where(VectorSyncOutbox.memory_id.in_(memory_ids))).scalars().all()
    return {"proxies": proxies, "memories": memories, "claims": claims, "revisions": revisions, "jobs": jobs, "events": events, "outbox": outbox}


def snapshot(run_id: str, output: Path) -> dict[str, Any]:
    require_safe_environment(); factory = build_sync_session_factory(); session = factory()
    try:
        rows = _scope(run_id, session); now = datetime.now(UTC)
        completed = [row for row in rows["jobs"] if row.completed_at is not None]
        queue_wait = [max(0.0, (row.processing_started_at - row.queued_at).total_seconds() * 1000) for row in rows["jobs"] if row.processing_started_at and row.queued_at]
        completion = [max(0.0, (row.completed_at - row.queued_at).total_seconds() * 1000) for row in completed if row.queued_at]
        pending_outbox = [row for row in rows["outbox"] if row.status != VectorSyncStatus.done]
        payload = {
            "schema_version": "1.0", "run_id": run_id, "captured_at": now.isoformat(), "holdout_used": False,
            "counts": {key: len(value) for key, value in rows.items()},
            "jobs": {"status": _counts(row.status.value for row in rows["jobs"]), "retries": sum(int(row.attempts or 0) for row in rows["jobs"]), "tokens": sum(int((row.result or {}).get("tokens_used", 0) or 0) for row in rows["jobs"]), "queue_wait_ms": _distribution(queue_wait), "completion_ms": _distribution(completion)},
            "outbox": {"status": _counts(row.status.value for row in rows["outbox"]), "oldest_pending_age_s": max([max(0.0, (now - row.created_at).total_seconds()) for row in pending_outbox] or [0.0])},
            "postgres": {"database_size_bytes": int(session.execute(select(func.pg_database_size(func.current_database()))).scalar_one())},
        }
        output.parent.mkdir(parents=True, exist_ok=True); output.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        print(json.dumps(payload, indent=2)); return payload
    finally: session.close()


def audit(run_id: str, output: Path) -> dict[str, Any]:
    require_safe_environment(); factory = build_sync_session_factory(); session = factory()
    try:
        rows = _scope(run_id, session); revision_by_claim: dict[uuid.UUID, list[Any]] = {}
        for revision in rows["revisions"]: revision_by_claim.setdefault(revision.claim_id, []).append(revision)
        duplicate_winners = sum(sum(row.status == "activated" for row in values) > 1 for values in revision_by_claim.values())
        winner_mismatch = sum(bool(claim.winning_revision_id) and not any(row.id == claim.winning_revision_id and row.status == "activated" for row in revision_by_claim.get(claim.id, [])) for claim in rows["claims"])
        duplicate_events = len(rows["events"]) - len({(str(row.tenant_id), row.source_service, row.source_event_id) for row in rows["events"]})
        provenance_missing = sum(memory.source_event_id is None or not (memory.metadata_json or {}).get("provenance") for memory in rows["memories"])
        broken_versions = sum(memory.previous_version_id is not None and memory.previous_version_id not in {row.id for row in rows["memories"]} for memory in rows["memories"])
        pending_outbox = sum(row.status != VectorSyncStatus.done for row in rows["outbox"])
        checks = {"single_winner": duplicate_winners == 0, "winner_alignment": winner_mismatch == 0, "event_idempotency": duplicate_events == 0, "provenance_preserved": provenance_missing == 0, "version_chain_integrity": broken_versions == 0, "outbox_converged": pending_outbox == 0}
        payload = {"schema_version": "1.0", "run_id": run_id, "captured_at": datetime.now(UTC).isoformat(), "holdout_used": False, "checks": checks, "violations": {"duplicate_winners": duplicate_winners, "winner_mismatch": winner_mismatch, "duplicate_source_events": duplicate_events, "missing_provenance": provenance_missing, "broken_versions": broken_versions, "pending_outbox": pending_outbox}, "passed": all(checks.values())}
        output.parent.mkdir(parents=True, exist_ok=True); output.write_text(json.dumps(payload, indent=2), encoding="utf-8"); print(json.dumps(payload, indent=2)); return payload
    finally: session.close()


def cleanup(run_id: str) -> dict[str, Any]:
    require_safe_environment(); factory = build_sync_session_factory(); session = factory(); qdrant = QdrantService(); deleted_points = 0
    try:
        rows = _scope(run_id, session)
        for memory in rows["memories"]:
            collections = {str((row.payload or {}).get("qdrant_collection") or qdrant.COLLECTION_NAME) for row in rows["outbox"] if row.memory_id == memory.id}
            for collection in collections or {qdrant.COLLECTION_NAME}:
                try: deleted_points += int(qdrant.delete_memory(str(memory.id), collection_name=collection))
                except Exception: pass
        proxy_ids = [row.id for row in rows["proxies"]]
        if proxy_ids: session.execute(delete(ProxyUser).where(ProxyUser.id.in_(proxy_ids)))
        session.commit()
        redis_client = _redis_client(); cursor = 0; deleted_keys = 0
        pattern = f"*{run_id}*"
        while True:
            cursor, keys = redis_client.scan(cursor=cursor, match=pattern, count=500)
            if keys: deleted_keys += redis_client.delete(*keys)
            if cursor == 0: break
        remaining = _scope(run_id, session)
        result = {"run_id": run_id, "deleted_proxy_users": len(proxy_ids), "deleted_qdrant_points": deleted_points, "deleted_redis_keys": deleted_keys, "remaining": {key: len(value) for key, value in remaining.items()}, "clean": all(not value for value in remaining.values())}
        print(json.dumps(result, indent=2)); return result
    finally: session.close()


def _counts(values: Any) -> dict[str, int]:
    result: dict[str, int] = {}
    for value in values: result[str(value)] = result.get(str(value), 0) + 1
    return result


def _distribution(values: list[float]) -> dict[str, float]:
    if not values: return {"count": 0, "p50": 0.0, "p95": 0.0, "p99": 0.0, "max": 0.0}
    ordered = sorted(values)
    def percentile(value: float) -> float: return round(ordered[min(len(ordered) - 1, max(0, int((len(ordered) - 1) * value)))], 2)
    return {"count": len(ordered), "p50": percentile(.50), "p95": percentile(.95), "p99": percentile(.99), "max": round(ordered[-1], 2)}


def main() -> None:
    parser = argparse.ArgumentParser(); parser.add_argument("command", choices=["preflight", "bootstrap", "snapshot", "audit", "cleanup"]); parser.add_argument("--run-id", required=True); parser.add_argument("--output", type=Path); args = parser.parse_args()
    if args.command == "preflight": require_safe_environment(); print(json.dumps({"safe": True, "run_id": args.run_id, "app_env": get_settings().app_env, "holdout_used": False}))
    elif args.command == "bootstrap": print(json.dumps(bootstrap()))
    elif args.command == "snapshot": snapshot(args.run_id, args.output or Path(f"artifacts/internal-benchmarks/scale/{args.run_id}/snapshot.json"))
    elif args.command == "audit": audit(args.run_id, args.output or Path(f"artifacts/internal-benchmarks/scale/{args.run_id}/audit.json"))
    else: cleanup(args.run_id)


if __name__ == "__main__": main()
