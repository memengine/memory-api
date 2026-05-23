from __future__ import annotations

import asyncio
import hashlib
import os
import random
import sys
import time
import uuid
from dataclasses import dataclass
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from pathlib import Path
from typing import Sequence
from unittest.mock import patch

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

try:
    from dotenv import load_dotenv

    load_dotenv(ROOT_DIR / ".env", override=False)
except Exception:
    pass

import redis.asyncio as redis
from sqlalchemy import func
from sqlalchemy import select

from api.db.cache import CacheService
from api.db.database import SessionLocal
from api.db.models import ApiKey
from api.db.models import Conversation
from api.db.models import EmbeddingModel
from api.db.models import Memory
from api.db.models import MemoryCategory
from api.db.models import ProxyUser
from api.db.models import Tenant
from api.db.vector_store import QdrantService
from api.services.context_builder import ContextBuilder
from api.services.embedding_service import DEFAULT_ACTIVE_MODEL_ID
from api.services.embedding_service import EmbeddingResult
from api.services.proxy_user_service import ProxyUserService
from api.services.quota_manager import QuotaManager
from api.services.retriever_service import RetrieverService
from api.utils.crypto import api_key_prefix
from api.utils.crypto import verify_api_key


def env_int(name: str, default: int) -> int:
    raw_value = os.getenv(name)
    if raw_value is None or raw_value.strip() == "":
        return default
    return int(raw_value)


def env_float(name: str, default: float) -> float:
    raw_value = os.getenv(name)
    if raw_value is None or raw_value.strip() == "":
        return default
    return float(raw_value)


EXTERNAL_USER_ID = os.getenv("BENCHMARK_EXTERNAL_USER_ID", "ajeet")
TARGET_MEMORY_COUNT = env_int("BENCHMARK_TARGET_MEMORY_COUNT", 50)
BENCHMARK_CALLS = env_int("BENCHMARK_CALLS", 100)
QUERY = os.getenv("BENCHMARK_QUERY", "programming language preferences")
VECTOR_SIZE = env_int("BENCHMARK_VECTOR_SIZE", 1536)
CACHED_P95_TARGET_MS = env_float("BENCHMARK_CACHED_P95_TARGET_MS", 300.0)
UNCACHED_P95_TARGET_MS = env_float("BENCHMARK_UNCACHED_P95_TARGET_MS", 500.0)

SYNTHETIC_MEMORIES: Sequence[tuple[str, str, float]] = (
    ("User builds SaaS products using Python and FastAPI", "expertise", 7.8),
    ("User prefers type-safe code and uses TypeScript for frontend", "preference", 7.2),
    ("User is working on a B2B SaaS targeting Indian SMBs", "goal", 8.1),
    ("User deploys using GitHub Actions to AWS ECS", "procedure", 6.9),
    ("User has 3 years of FastAPI experience", "expertise", 7.5),
    ("User prefers dark mode and vim keybindings", "preference", 5.2),
    ("User is learning Rust for systems programming", "goal", 6.4),
    ("User uses PostgreSQL for primary database", "expertise", 7.0),
    ("User works remotely and is based in Bangalore", "fact", 6.1),
    ("User has a technical co-founder named Raj", "relationship", 5.8),
    ("User prefers concise implementation-focused explanations", "preference", 7.9),
    ("User is building MemoryOS for AI memory infrastructure", "goal", 8.8),
    ("User uses Docker Compose for local backend development", "procedure", 6.7),
    ("User debugs APIs using PowerShell Invoke-RestMethod", "procedure", 5.9),
    ("User prefers Python-first examples for backend code", "preference", 8.0),
    ("User is interested in cloud security for fintech clients", "goal", 7.4),
    ("User understands PostgreSQL migrations and Alembic workflows", "expertise", 7.1),
    ("User tracks Qdrant collections for vector search verification", "procedure", 6.8),
    ("User wants production dashboards to show useful error details", "preference", 7.3),
    ("User is improving conflict resolution for cross-user memory", "goal", 8.2),
    ("User uses Gemini models for extraction experiments", "fact", 6.0),
    ("User prefers direct pass/fail verification output", "preference", 6.6),
    ("User is learning how degraded retrieval should behave", "goal", 6.8),
    ("User has experience with tenant and operator dashboards", "expertise", 7.6),
    ("User wants documentation updated after feature changes", "preference", 6.5),
    ("User is testing GDPR delete and export workflows", "procedure", 6.1),
    ("User builds Next.js dashboards for MemoryOS", "expertise", 7.0),
    ("User prefers local deterministic benchmarks before provider benchmarks", "preference", 7.7),
    ("User uses Redis for cache and hot tier behavior", "expertise", 6.9),
    ("User monitors dead letter queues for extraction failures", "procedure", 6.4),
    ("User is based in India and works in IST timezone", "fact", 5.5),
    ("User wants clear onboarding flows for consent dashboard", "goal", 6.7),
    ("User cares about tenant isolation and cross-agent security", "preference", 8.3),
    ("User experiments with FastAPI middleware and auth headers", "expertise", 6.6),
    ("User wants benchmark targets under 300ms cached and 500ms uncached", "goal", 8.5),
    ("User uses Supabase SQL editor for manual table checks", "procedure", 5.8),
    ("User values high-signal concise summaries after tests", "preference", 7.1),
    ("User is validating memory retrieval relevance across categories", "goal", 7.2),
    ("User is building universal memory sharing with consent grants", "expertise", 8.0),
    ("User tests SMTP OTP flows for Memory Passport login", "procedure", 5.7),
    ("User wants operator health to reflect real LLM provider state", "goal", 7.6),
    ("User prefers keeping temporary benchmark data clearly tagged", "preference", 6.2),
    ("User uses pytest for service-level verification", "procedure", 6.3),
    ("User works with Celery workers for extraction tasks", "expertise", 6.8),
    ("User wants fallback behavior to be verified end-to-end", "goal", 8.4),
    ("User is interested in retrieval scoring using semantic importance and recency", "expertise", 7.4),
    ("User wants tenant admins to see conflict stats without noisy alerts", "preference", 6.9),
    ("User uses Qdrant cosine vectors with 1536 dimensions", "expertise", 6.5),
    ("User wants production tests to avoid external API latency", "preference", 8.1),
    ("User treats documentation as part of shipping features", "preference", 7.0),
)


@dataclass(frozen=True)
class BenchmarkTarget:
    tenant: Tenant
    proxy_user: ProxyUser
    user_id: uuid.UUID
    source_conversation_id: uuid.UUID
    embedding_model_id: str
    qdrant_collection: str


class MockEmbeddingService:
    async def embed(self, text: str, model_id: str | None = None) -> EmbeddingResult:
        return EmbeddingResult(
            vector=deterministic_vector(text),
            model_id=model_id or DEFAULT_ACTIVE_MODEL_ID,
            dimensions=VECTOR_SIZE,
            qdrant_collection="memories",
        )

    async def get_active_model(self) -> object:
        return type("ActiveModel", (), {"id": DEFAULT_ACTIVE_MODEL_ID})()


class NoopAccessUpdateTask:
    def delay(self, memory_ids: list[str]) -> None:
        del memory_ids


def deterministic_vector(text: str) -> list[float]:
    hash_bytes = hashlib.sha256(text.encode("utf-8")).digest()
    seed = int.from_bytes(hash_bytes[:8], "big")
    rng = random.Random(seed)
    vector = [rng.gauss(0, 1) for _ in range(VECTOR_SIZE)]
    magnitude = sum(item * item for item in vector) ** 0.5
    return [item / magnitude for item in vector]


def percentile(data: Sequence[float], pct: int) -> float:
    sorted_data = sorted(data)
    index = int(len(sorted_data) * pct / 100)
    return sorted_data[min(index, len(sorted_data) - 1)]


async def resolve_tenant(session) -> Tenant | None:
    raw_api_key = os.getenv("BENCHMARK_API_KEY")
    if raw_api_key:
        result = await session.execute(
            select(ApiKey).where(
                ApiKey.is_active.is_(True),
                ApiKey.key_prefix == api_key_prefix(raw_api_key),
            )
        )
        for key in result.scalars().all():
            if verify_api_key(raw_api_key, key.key_hash):
                return await session.get(Tenant, key.tenant_id)
        print("ERROR: BENCHMARK_API_KEY did not match an active tenant API key.")
        return None

    result = await session.execute(
        select(Tenant)
        .join(ProxyUser, ProxyUser.tenant_id == Tenant.id)
        .where(Tenant.is_active.is_(True), ProxyUser.external_user_id == EXTERNAL_USER_ID)
        .limit(1)
    )
    tenant = result.scalar_one_or_none()
    if tenant is not None:
        return tenant

    result = await session.execute(select(Tenant).where(Tenant.is_active.is_(True)).limit(1))
    return result.scalar_one_or_none()


async def resolve_target(cache_service: CacheService, qdrant_service: QdrantService) -> BenchmarkTarget:
    async with SessionLocal() as session:
        tenant = await resolve_tenant(session)
        if tenant is None:
            raise SystemExit("ERROR: No active tenant found for benchmark.")

        proxy_user_service = ProxyUserService(
            session=session,
            cache_service=cache_service,
            qdrant_service=qdrant_service,
        )
        proxy_user = await proxy_user_service.resolve(str(tenant.id), EXTERNAL_USER_ID)
        print(f"Resolved {EXTERNAL_USER_ID} -> proxy_user_id: {proxy_user.id}")

        result = await session.execute(
            select(Memory)
            .where(Memory.proxy_user_id == proxy_user.id, Memory.is_archived.is_(False))
            .order_by(Memory.created_at.asc())
            .limit(1)
        )
        memory = result.scalar_one_or_none()
        if memory is None:
            raise SystemExit(
                "ERROR: ajeet has no existing memory. Add one real memory first so "
                "the benchmark can reuse the correct user/conversation foreign keys."
            )

        model = await session.get(EmbeddingModel, memory.embedding_model_id)
        qdrant_collection = model.qdrant_collection if model else "memories"

        return BenchmarkTarget(
            tenant=tenant,
            proxy_user=proxy_user,
            user_id=memory.user_id,
            source_conversation_id=memory.source_conversation_id,
            embedding_model_id=memory.embedding_model_id,
            qdrant_collection=qdrant_collection,
        )


async def active_memory_count(proxy_user_id: uuid.UUID) -> int:
    async with SessionLocal() as session:
        result = await session.execute(
            select(func.count())
            .select_from(Memory)
            .where(Memory.proxy_user_id == proxy_user_id, Memory.is_archived.is_(False))
        )
        return int(result.scalar_one())


async def ensure_memories(target: BenchmarkTarget, qdrant_service: QdrantService) -> int:
    count = await active_memory_count(target.proxy_user.id)
    print(f"{EXTERNAL_USER_ID} has {count} active memories")
    if count >= TARGET_MEMORY_COUNT:
        await sync_benchmark_qdrant_payloads(target, qdrant_service)
        return count

    to_insert = TARGET_MEMORY_COUNT - count
    print(f"Seeding {to_insert} memories...")
    now = datetime.now(UTC)
    inserted = 0

    async with SessionLocal() as session:
        for index in range(to_insert):
            content, category, importance = SYNTHETIC_MEMORIES[index % len(SYNTHETIC_MEMORIES)]
            memory_id = uuid.uuid4()
            created_at = now - timedelta(days=(index * 17) % 181)
            last_accessed_at = now - timedelta(days=(index * 7) % 31)
            access_count = (index * 3) % 21
            memory = Memory(
                id=memory_id,
                user_id=target.user_id,
                proxy_user_id=target.proxy_user.id,
                agent_id=None,
                content=f"{content} [benchmark-{index}]",
                category=MemoryCategory(category),
                importance_score=float(importance),
                confidence_score=0.85,
                embedding_id=f"benchmark-{memory_id}",
                embedding_model_id=target.embedding_model_id,
                source_conversation_id=target.source_conversation_id,
                metadata_json={"benchmark_seed": True},
                access_count=access_count,
                created_at=created_at,
                updated_at=created_at,
                last_accessed_at=last_accessed_at,
                is_archived=False,
            )
            session.add(memory)
            qdrant_service.upsert_memory(
                str(memory_id),
                deterministic_vector(memory.content),
                {
                    "memory_id": str(memory_id),
                    "tenant_id": str(target.tenant.id),
                    "proxy_user_id": str(target.proxy_user.id),
                    "user_id": str(target.user_id),
                    "category": category,
                    "importance_score": float(importance),
                    "is_archived": False,
                    "created_at": created_at.isoformat(),
                    "embedding_model_id": target.embedding_model_id,
                    "qdrant_collection": target.qdrant_collection,
                },
                collection_name=target.qdrant_collection,
                vector_size=VECTOR_SIZE,
            )
            inserted += 1

        await session.commit()

    total = await active_memory_count(target.proxy_user.id)
    await sync_benchmark_qdrant_payloads(target, qdrant_service)
    print(f"Seeded {inserted} memories. Total: {total}")
    return total


async def sync_benchmark_qdrant_payloads(target: BenchmarkTarget, qdrant_service: QdrantService) -> None:
    async with SessionLocal() as session:
        result = await session.execute(
            select(Memory)
            .where(
                Memory.proxy_user_id == target.proxy_user.id,
                Memory.is_archived.is_(False),
            )
            .order_by(Memory.importance_score.desc())
            .limit(TARGET_MEMORY_COUNT)
        )
        memories = list(result.scalars().all())

    for memory in memories:
        qdrant_service.upsert_memory(
            str(memory.id),
            deterministic_vector(memory.content),
            {
                "memory_id": str(memory.id),
                "tenant_id": str(target.tenant.id),
                "proxy_user_id": str(target.proxy_user.id),
                "user_id": str(target.user_id),
                "agent_id": str(memory.agent_id) if memory.agent_id else None,
                "content": memory.content,
                "category": memory.category.value if hasattr(memory.category, "value") else str(memory.category),
                "importance_score": float(memory.importance_score),
                "confidence_score": float(memory.confidence_score),
                "is_archived": False,
                "created_at": memory.created_at.isoformat() if memory.created_at else None,
                "last_accessed_at": memory.last_accessed_at.isoformat() if memory.last_accessed_at else None,
                "previous_version_id": str(memory.previous_version_id) if memory.previous_version_id else None,
                "embedding_model_id": target.embedding_model_id,
                "qdrant_collection": target.qdrant_collection,
            },
            collection_name=target.qdrant_collection,
            vector_size=VECTOR_SIZE,
        )


async def build_retriever(
    *,
    cache_service: CacheService,
    qdrant_service: QdrantService,
) -> tuple[RetrieverService, object]:
    session = SessionLocal()
    quota_manager = QuotaManager(
        session=session,
        cache_service=cache_service,
        dispatch_task=lambda *args, **kwargs: None,
    )
    proxy_user_service = ProxyUserService(
        session=session,
        cache_service=cache_service,
        qdrant_service=qdrant_service,
    )
    retriever = RetrieverService(
        session=session,
        cache_service=cache_service,
        qdrant_service=qdrant_service,
        quota_manager=quota_manager,
        proxy_user_service=proxy_user_service,
        embedding_service=MockEmbeddingService(),
    )
    return retriever, session


async def retrieve_once(
    *,
    query: str,
    target: BenchmarkTarget,
    cache_service: CacheService,
    qdrant_service: QdrantService,
) -> tuple[float, bool, int, int]:
    retriever, session = await build_retriever(
        cache_service=cache_service,
        qdrant_service=qdrant_service,
    )
    try:
        start = time.perf_counter()
        memories = await retriever.retrieve(
            query=query,
            external_user_id=EXTERNAL_USER_ID,
            proxy_user_id=str(target.proxy_user.id),
            tenant_id=str(target.tenant.id),
            limit=10,
            quota_mode="full",
        )
        context = ContextBuilder().build(memories, format="bullets", max_tokens=500)
        elapsed_ms = (time.perf_counter() - start) * 1000
        return elapsed_ms, retriever.last_cache_hit, len(memories), len(context.system_prompt_addition)
    finally:
        await session.close()


async def run_cached_calls(
    *,
    target: BenchmarkTarget,
    cache_service: CacheService,
    qdrant_service: QdrantService,
) -> list[float]:
    async def single_cached_call() -> float:
        elapsed_ms, cached, _, _ = await retrieve_once(
            query=QUERY,
            target=target,
            cache_service=cache_service,
            qdrant_service=qdrant_service,
        )
        if not cached:
            raise AssertionError("Expected cache hit - got cache miss")
        return elapsed_ms

    return await asyncio.gather(*(single_cached_call() for _ in range(BENCHMARK_CALLS)))


async def run_uncached_calls(
    *,
    target: BenchmarkTarget,
    cache_service: CacheService,
    qdrant_service: QdrantService,
) -> list[float]:
    queries = [f"benchmark query {index} {uuid.uuid4().hex[:8]}" for index in range(BENCHMARK_CALLS)]

    async def single_uncached_call(query: str) -> float:
        elapsed_ms, _, _, _ = await retrieve_once(
            query=query,
            target=target,
            cache_service=cache_service,
            qdrant_service=qdrant_service,
        )
        return elapsed_ms

    return await asyncio.gather(*(single_uncached_call(query) for query in queries))


def print_results(memory_count: int, cached: Sequence[float], uncached: Sequence[float]) -> int:
    cached_p50 = percentile(cached, 50)
    cached_p95 = percentile(cached, 95)
    cached_p99 = percentile(cached, 99)
    cached_max = max(cached)

    uncached_p50 = percentile(uncached, 50)
    uncached_p95 = percentile(uncached, 95)
    uncached_p99 = percentile(uncached, 99)
    uncached_max = max(uncached)

    print("\n" + "=" * 60)
    print("MEMORYOS RETRIEVER LATENCY BENCHMARK")
    print("=" * 60)
    print(f"User: {EXTERNAL_USER_ID} | Memories: {memory_count} | Calls: {BENCHMARK_CALLS} each")
    print("Embedding: deterministic mock (no API calls)")
    print()
    print("CACHED RESULTS (same query, Redis hit):")
    print(f"  p50: {cached_p50:.1f}ms")
    print(
        f"  p95: {cached_p95:.1f}ms  "
        f"{'PASS' if cached_p95 < CACHED_P95_TARGET_MS else 'FAIL'} "
        f"(target: < {CACHED_P95_TARGET_MS:.0f}ms)"
    )
    print(f"  p99: {cached_p99:.1f}ms")
    print(f"  max: {cached_max:.1f}ms")
    print()
    print("UNCACHED RESULTS (unique queries, Qdrant search):")
    print(f"  p50: {uncached_p50:.1f}ms")
    print(
        f"  p95: {uncached_p95:.1f}ms  "
        f"{'PASS' if uncached_p95 < UNCACHED_P95_TARGET_MS else 'FAIL'} "
        f"(target: < {UNCACHED_P95_TARGET_MS:.0f}ms)"
    )
    print(f"  p99: {uncached_p99:.1f}ms")
    print(f"  max: {uncached_max:.1f}ms")
    print()
    print("BREAKDOWN (uncached call components):")
    print("  mock_embed:    ~0ms   (deterministic, no API)")
    print("  Qdrant search: included in uncached latency")
    print("  PostgreSQL:    included in uncached latency")
    print("  re-ranking:    included in uncached latency")
    print("  ContextBuilder: included in both cached and uncached latency")
    print()

    all_pass = cached_p95 < CACHED_P95_TARGET_MS and uncached_p95 < UNCACHED_P95_TARGET_MS
    if all_pass:
        print("RESULT: PASS - ALL TARGETS MET")
    else:
        print("RESULT: FAIL - TARGETS NOT MET")
        if cached_p95 >= CACHED_P95_TARGET_MS:
            print(f"  FAIL: Cached p95 {cached_p95:.1f}ms >= {CACHED_P95_TARGET_MS:.0f}ms")
            print("  Check: Redis connection, cache serialisation")
        if uncached_p95 >= UNCACHED_P95_TARGET_MS:
            print(f"  FAIL: Uncached p95 {uncached_p95:.1f}ms >= {UNCACHED_P95_TARGET_MS:.0f}ms")
            print("  Check: Qdrant performance, PostgreSQL query plan")
            print("  Run: EXPLAIN ANALYZE on the memories query")
            print("  Check: missing index on proxy_user_id + is_archived")
    print("=" * 60)
    return 0 if all_pass else 1


async def main() -> int:
    redis_url = os.getenv("BENCHMARK_REDIS_URL") or os.getenv("REDIS_URL") or "redis://localhost:6379/0"
    qdrant_url = os.getenv("BENCHMARK_QDRANT_URL") or os.getenv("QDRANT_URL")
    qdrant_api_key = os.getenv("BENCHMARK_QDRANT_API_KEY")
    redis_client = redis.from_url(
        redis_url,
        encoding="utf-8",
        decode_responses=True,
        socket_connect_timeout=2.0,
        socket_timeout=2.0,
    )
    cache_service = CacheService(client=redis_client, use_direct_breaker=True)
    qdrant_service = QdrantService(url=qdrant_url, api_key=qdrant_api_key)
    target = await resolve_target(cache_service, qdrant_service)
    memory_count = await ensure_memories(target, qdrant_service)

    print("Warming cache...")
    await cache_service.invalidate_user_cache(str(target.proxy_user.id))
    _, warmed_cached, warm_count, context_chars = await retrieve_once(
        query=QUERY,
        target=target,
        cache_service=cache_service,
        qdrant_service=qdrant_service,
    )
    print(f"Cache warmed. {warm_count} memories returned.")
    print(f"system_prompt_addition: {context_chars} chars")
    if warmed_cached:
        print("WARNING: Warm call unexpectedly hit cache; benchmark will continue.")
    if warm_count == 0:
        print("WARNING: No memories returned. Check Qdrant.")
        print("Benchmark will continue but results may be misleading.")

    print(f"Running {BENCHMARK_CALLS} parallel cached calls...")
    cached_latencies = await run_cached_calls(
        target=target,
        cache_service=cache_service,
        qdrant_service=qdrant_service,
    )

    print(f"Running {BENCHMARK_CALLS} parallel uncached calls...")
    uncached_latencies = await run_uncached_calls(
        target=target,
        cache_service=cache_service,
        qdrant_service=qdrant_service,
    )

    return print_results(memory_count, cached_latencies, uncached_latencies)


if __name__ == "__main__":
    with patch("api.services.retriever.update_memory_accesses", NoopAccessUpdateTask()):
        sys.exit(asyncio.run(main()))
