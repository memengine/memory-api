from __future__ import annotations

import asyncio
import inspect
import logging
import os
import uuid
from collections.abc import Iterable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from api.db.cache import CacheService
from api.db.models import Memory, QuotaMode
from api.db.vector_store import QdrantService
from api.errors import APIError
from api.infra.circuit_breaker_registry import CircuitBreakerRegistry
from api.services.embedding_service import EmbeddingResult, EmbeddingService
from api.services.proxy_user_service import ProxyUserService
from api.services.quota_manager import QuotaManager
from api.tasks.retrieval_tasks import update_memory_accesses

CACHE_TTL_SECONDS = 60
L1_CACHE_TTL_SECONDS = float(os.getenv("RETRIEVAL_L1_CACHE_TTL_SECONDS", "1.0"))
MODEL_ID_CACHE_TTL_SECONDS = float(os.getenv("RETRIEVAL_MODEL_ID_CACHE_TTL_SECONDS", "60.0"))
MEMORY_COUNT_CACHE_TTL_SECONDS = float(os.getenv("RETRIEVAL_MEMORY_COUNT_CACHE_TTL_SECONDS", "30.0"))
HOT_TIER_CACHE_TTL_SECONDS = float(os.getenv("RETRIEVAL_HOT_TIER_CACHE_TTL_SECONDS", "2.0"))
REDIS_CACHE_READ_TIMEOUT_SECONDS = float(os.getenv("RETRIEVAL_CACHE_READ_TIMEOUT_SECONDS", "0.05"))
REDIS_CACHE_READ_ENABLED = os.getenv("RETRIEVAL_REDIS_CACHE_READ_ENABLED", "true").lower() in {"1", "true", "yes"}
REDIS_CACHE_WRITE_ENABLED = os.getenv("RETRIEVAL_REDIS_CACHE_WRITE_ENABLED", "true").lower() in {"1", "true", "yes"}
OVERFETCH_MULTIPLIER = max(1, int(os.getenv("RETRIEVAL_OVERFETCH_MULTIPLIER", "3")))
MIN_SEMANTIC_SCORE = min(1.0, max(0.0, float(os.getenv("RETRIEVAL_MIN_SEMANTIC_SCORE", "0.315"))))
COLD_START_THRESHOLD = 5
DEDUPLICATION_THRESHOLD = 0.95


logger = logging.getLogger(__name__)


def _retrieval_weight_from_env(name: str, default: str) -> float:
    return float(os.getenv(name, default))


SEMANTIC_WEIGHT = _retrieval_weight_from_env("RETRIEVAL_SEMANTIC_WEIGHT", "0.60")
IMPORTANCE_WEIGHT = _retrieval_weight_from_env("RETRIEVAL_IMPORTANCE_WEIGHT", "0.25")
RECENCY_WEIGHT = _retrieval_weight_from_env("RETRIEVAL_RECENCY_WEIGHT", "0.15")
RETRIEVAL_WEIGHT_TOTAL = SEMANTIC_WEIGHT + IMPORTANCE_WEIGHT + RECENCY_WEIGHT
if abs(RETRIEVAL_WEIGHT_TOTAL - 1.0) > 0.001:
    raise ValueError(f"Retrieval weights must sum to 1.0, got {RETRIEVAL_WEIGHT_TOTAL}")


@dataclass(slots=True)
class MemoryResult:
    id: str
    content: str
    category: str
    importance_score: float
    confidence_score: float
    semantic_score: float
    recency_score: float
    final_score: float
    agent_id: str | None
    previous_version_id: str | None
    last_accessed_at: str | None
    created_at: str | None = None
    source_event_id: str | None = None
    provenance: dict[str, Any] | None = None
    effective_from: str | None = None
    effective_until: str | None = None


class RetrieverService:
    SEMANTIC_WEIGHT = SEMANTIC_WEIGHT
    IMPORTANCE_WEIGHT = IMPORTANCE_WEIGHT
    RECENCY_WEIGHT = RECENCY_WEIGHT
    _l1_cache: dict[str, tuple[float, list[MemoryResult]]] = {}
    _model_id_cache: dict[str, tuple[float, list[str]]] = {}
    _memory_count_cache: dict[str, tuple[float, int]] = {}
    _hot_tier_cache: dict[str, tuple[float, list[MemoryResult]]] = {}

    def __init__(
        self,
        *,
        session: AsyncSession,
        qdrant_service: QdrantService,
        cache_service: CacheService,
        quota_manager: QuotaManager,
        proxy_user_service: ProxyUserService | None = None,
        embedding_service: EmbeddingService | None = None,
        client: Any | None = None,
        region_id: str | None = None,
    ) -> None:
        self.session = session
        self.qdrant_service = qdrant_service
        self.cache_service = cache_service
        self.quota_manager = quota_manager
        self.proxy_user_service = proxy_user_service
        self.embed_breaker = CircuitBreakerRegistry.get_instance().gemini_embed_cb
        self.last_cache_hit = False
        self.last_quota_mode = QuotaMode.full.value
        self.last_is_degraded = False
        self.region_id = region_id
        self.embedding_service = embedding_service or EmbeddingService(
            async_session=session,
            gemini_client=client,
        )

    @classmethod
    def invalidate_local_user_cache(cls, user_id: str) -> None:
        """Invalidate only process-local entries owned by one user/proxy identity."""
        prefix = f"{user_id}|"
        for cache in (cls._l1_cache, cls._hot_tier_cache):
            for key in [key for key in cache if key.startswith(prefix)]:
                cache.pop(key, None)
        cls._memory_count_cache.pop(f"proxy:{user_id}", None)
        cls._memory_count_cache.pop(f"user:{user_id}", None)

    async def retrieve(
        self,
        query: str,
        external_user_id: str | None = None,
        proxy_user_id: str | None = None,
        user_id: str | None = None,
        limit: int = 10,
        categories: list[str] | None = None,
        agent_id: str | None = None,
        tenant_id: str | None = None,
        time_filter_days: int | None = None,
        as_of: datetime | None = None,
        quota_mode: str | QuotaMode | None = None,
    ) -> list[MemoryResult]:
        identity = external_user_id or user_id
        if not identity:
            raise ValueError("retrieve requires external_user_id or user_id.")
        normalized_categories = [category.lower() for category in categories or []]
        created_after = self._created_after(time_filter_days)
        cache_context = self._cache_context(query, normalized_categories, agent_id, limit, time_filter_days, as_of)

        self.last_is_degraded = False

        proxy_user = None
        cache_identity = identity
        if tenant_id:
            if self.proxy_user_service is not None:
                if proxy_user_id:
                    cache_identity = str(proxy_user_id)
                    proxy_user = SimpleNamespace(id=uuid.UUID(str(proxy_user_id)))
                else:
                    proxy_user = await self.proxy_user_service.resolve(
                        tenant_id=tenant_id,
                        external_user_id=identity,
                    )
                    cache_identity = str(proxy_user.id)
            elif user_id is not None:
                cache_identity = user_id

        cached_results = await self._get_cached_results(user_id=cache_identity, cache_context=cache_context)
        if cached_results is not None:
            cached_results = self._filter_current_results(cached_results)
            self.last_cache_hit = True
            self.last_quota_mode = self._coerce_quota_mode(quota_mode).value if quota_mode is not None else QuotaMode.full.value
            self._queue_access_update([result.id for result in cached_results])
            return cached_results[:limit]
        self.last_cache_hit = False

        resolved_quota_mode = self._coerce_quota_mode(quota_mode) if quota_mode is not None else await self._resolve_quota_mode(tenant_id)
        self.last_quota_mode = resolved_quota_mode.value

        if resolved_quota_mode in {QuotaMode.blocked, QuotaMode.passthrough}:
            return []

        if as_of is not None:
            historical_results = await self._retrieve_as_of_semantic(
                query=query,
                tenant_id=tenant_id,
                proxy_user_id=str(proxy_user.id) if proxy_user is not None else None,
                user_id=identity if proxy_user is None else None,
                as_of=as_of,
                limit=limit,
                categories=normalized_categories,
                agent_id=agent_id,
                created_after=created_after,
            )
            self._queue_access_update([result.id for result in historical_results])
            return historical_results

        # Current reads enforce effective_from/effective_until in every candidate source.
        hot_tier_results = await self._get_hot_tier_results(
            proxy_user_id=str(proxy_user.id) if proxy_user is not None else None,
            categories=normalized_categories,
            agent_id=agent_id,
            created_after=created_after,
        )

        if resolved_quota_mode == QuotaMode.degraded_retrieve:
            self.last_is_degraded = True
            self._queue_access_update([result.id for result in hot_tier_results])
            return hot_tier_results[:limit]

        if proxy_user is not None:
            scoped_identity = str(proxy_user.id)
            user_memory_count = await self._count_proxy_user_memories(proxy_user_id=scoped_identity)
        else:
            scoped_identity = identity
            user_memory_count = await self._count_user_memories(user_id=scoped_identity)
        if user_memory_count < COLD_START_THRESHOLD:
            if proxy_user is not None:
                cold_start_results = await self._retrieve_cold_start_memories(
                    proxy_user_id=scoped_identity,
                    limit=max(0, limit - len(hot_tier_results)),
                    categories=normalized_categories,
                    agent_id=agent_id,
                    created_after=created_after,
                )
            else:
                cold_start_results = await self._retrieve_cold_start_memories_for_user(
                    user_id=scoped_identity,
                    limit=max(0, limit - len(hot_tier_results)),
                    categories=normalized_categories,
                    agent_id=agent_id,
                    created_after=created_after,
                )
            final_results = self._merge_hot_tier_results(hot_tier_results, cold_start_results, limit)
            self._queue_access_update([result.id for result in final_results])
            await self._cache_results(user_id=cache_identity, results=final_results, cache_context=cache_context)
            return final_results

        if len(hot_tier_results) >= limit:
            self._queue_access_update([result.id for result in hot_tier_results[:limit]])
            await self._cache_results(user_id=cache_identity, results=hot_tier_results[:limit], cache_context=cache_context)
            return hot_tier_results[:limit]

        remaining_limit = max(0, limit - len(hot_tier_results))
        if self.qdrant_service.breaker.current_state() == "OPEN":
            self.last_is_degraded = True
            logger.warning(
                "Qdrant circuit already open; skipping embeddings and vector search. tenant_id=%s proxy_user_id=%s",
                tenant_id,
                scoped_identity if proxy_user is not None else None,
            )
            fallback_results = await self._retrieve_postgres_fallback(
                proxy_user_id=scoped_identity if proxy_user is not None else None,
                user_id=scoped_identity if proxy_user is None else None,
                limit=max(0, limit - len(hot_tier_results)),
                categories=normalized_categories,
                agent_id=agent_id,
                created_after=created_after,
            )
            final_results = self._merge_hot_tier_results(hot_tier_results, fallback_results, limit)
            self._queue_access_update([result.id for result in final_results])
            await self._cache_results(user_id=cache_identity, results=final_results, cache_context=cache_context)
            return final_results

        model_ids = await self._candidate_embedding_model_ids(
            proxy_user_id=str(proxy_user.id) if proxy_user is not None else None,
            user_id=scoped_identity if proxy_user is None else None,
        )

        try:
            query_embeddings = [
                await self._embed_query(query, model_id=model_id)
                for model_id in model_ids
            ]
        except Exception as exc:
            logger.warning(
                "Retriever embedding failed; returning dependency error. tenant_id=%s proxy_user_id=%s error=%s",
                tenant_id,
                scoped_identity if proxy_user is not None else None,
                exc,
            )
            raise APIError(
                status_code=503,
                code="EMB_503",
                error="embedding_unavailable",
                details={"reason": str(exc)},
            ) from exc
        if proxy_user is not None:
            scored_points = []
            for query_embedding in query_embeddings:
                scored_points.extend(
                    await self._search_qdrant(
                        query_embedding=query_embedding.vector,
                        tenant_id=tenant_id,
                        proxy_user_id=scoped_identity,
                        limit=min(max(remaining_limit * OVERFETCH_MULTIPLIER, remaining_limit), 50),
                        categories=normalized_categories,
                        agent_id=agent_id,
                        collection_name=query_embedding.qdrant_collection,
                        created_after=created_after,
                    )
                )
        else:
            scored_points = []
            for query_embedding in query_embeddings:
                scored_points.extend(
                    await self._search_qdrant_legacy(
                        query_embedding=query_embedding.vector,
                        user_id=scoped_identity,
                        limit=min(max(remaining_limit * OVERFETCH_MULTIPLIER, remaining_limit), 50),
                        categories=normalized_categories,
                        agent_id=agent_id,
                        collection_name=query_embedding.qdrant_collection,
                        created_after=created_after,
                    )
                )

        if not scored_points and self.qdrant_service.breaker.current_state() == "OPEN":
            self.last_is_degraded = True
            fallback_results = await self._retrieve_postgres_fallback(
                proxy_user_id=scoped_identity if proxy_user is not None else None,
                user_id=scoped_identity if proxy_user is None else None,
                limit=max(0, limit - len(hot_tier_results)),
                categories=normalized_categories,
                agent_id=agent_id,
                created_after=created_after,
            )
            final_results = self._merge_hot_tier_results(hot_tier_results, fallback_results, limit)
            self._queue_access_update([result.id for result in final_results])
            await self._cache_results(user_id=cache_identity, results=final_results, cache_context=cache_context)
            return final_results

        scored_points = [
            point
            for point in scored_points
            if self._passes_semantic_floor(point)
            and self._payload_is_current(getattr(point, "payload", {}) or {})
        ]

        if not scored_points:
            self._queue_access_update([result.id for result in hot_tier_results])
            return hot_tier_results[:limit]

        payload_results = self._results_from_qdrant_payloads(scored_points, agent_id=agent_id)
        if payload_results:
            deduplicated_results = self._deduplicate_results(payload_results)
            ranked_results = sorted(
                deduplicated_results,
                key=lambda item: item.final_score,
                reverse=True,
            )
            final_results = self._merge_hot_tier_results(hot_tier_results, ranked_results, limit)
            self._queue_access_update([result.id for result in final_results])
            await self._cache_results(user_id=cache_identity, results=final_results, cache_context=cache_context)
            return final_results

        top_memory_ids = [self._point_memory_id(point) for point in scored_points]
        if proxy_user is not None:
            memories_by_id = await self._fetch_memories_by_ids(
                memory_ids=top_memory_ids,
                proxy_user_id=scoped_identity,
                categories=normalized_categories,
                agent_id=agent_id,
                created_after=created_after,
            )
        else:
            memories_by_id = await self._fetch_memories_by_ids_for_user(
                memory_ids=top_memory_ids,
                user_id=scoped_identity,
                categories=normalized_categories,
                agent_id=agent_id,
                created_after=created_after,
            )

        scored_results: list[MemoryResult] = []
        for point in scored_points:
            memory_id = self._point_memory_id(point)
            if memory_id is None:
                continue

            memory = memories_by_id.get(memory_id)
            if memory is None:
                continue

            semantic_score = float(getattr(point, "score", 0.0) or 0.0)
            recency_score = self._recency_score(memory.last_accessed_at)
            final_score = (
                (self.SEMANTIC_WEIGHT * semantic_score)
                + (self.IMPORTANCE_WEIGHT * (float(memory.importance_score) / 10.0))
                + (self.RECENCY_WEIGHT * recency_score)
            )

            scored_results.append(
                MemoryResult(
                    id=str(memory.id),
                    content=memory.content,
                    category=memory.category.value,
                    importance_score=float(memory.importance_score),
                    confidence_score=float(memory.confidence_score),
                    semantic_score=semantic_score,
                    recency_score=recency_score,
                    final_score=round(final_score, 6),
                    agent_id=str(memory.agent_id) if memory.agent_id else None,
                    previous_version_id=(
                        str(memory.previous_version_id) if memory.previous_version_id else None
                    ),
                    last_accessed_at=(
                        memory.last_accessed_at.isoformat() if memory.last_accessed_at else None
                    ),
                    created_at=memory.created_at.isoformat() if memory.created_at else None,
                    source_event_id=str(memory.source_event_id) if memory.source_event_id else None,
                    provenance=(memory.metadata_json or {}).get("provenance"),
                    effective_from=memory.effective_from.isoformat() if memory.effective_from else None,
                    effective_until=memory.effective_until.isoformat() if memory.effective_until else None,
                )
            )

        deduplicated_results = self._deduplicate_results(scored_results)
        ranked_results = sorted(
            deduplicated_results,
            key=lambda item: item.final_score,
            reverse=True,
        )
        final_results = self._merge_hot_tier_results(hot_tier_results, ranked_results, limit)

        self._queue_access_update([result.id for result in final_results])
        await self._cache_results(user_id=cache_identity, results=final_results, cache_context=cache_context)
        return final_results

    async def _resolve_quota_mode(self, tenant_id: str | None) -> QuotaMode:
        if not tenant_id:
            return QuotaMode.full
        try:
            return await self.quota_manager.get_mode(tenant_id)
        except Exception as exc:
            logger.warning(
                "QuotaManager.get_mode failed; defaulting to FULL. tenant_id=%s error=%s",
                tenant_id,
                exc,
            )
            return QuotaMode.full

    @staticmethod
    def _coerce_quota_mode(value: str | QuotaMode | None) -> QuotaMode:
        if isinstance(value, QuotaMode):
            return value
        if value is None:
            return QuotaMode.full
        normalized = str(value).strip().lower()
        for mode in QuotaMode:
            if normalized in {mode.value.lower(), mode.name.lower()}:
                return mode
        return QuotaMode.full

    async def _get_cached_results(
        self,
        *,
        user_id: str,
        cache_context: str,
    ) -> list[MemoryResult] | None:
        l1_key = self._l1_cache_key(user_id, cache_context)
        l1_cached = self._l1_cache.get(l1_key)
        now = datetime.now(UTC).timestamp()
        if l1_cached is not None:
            expires_at, results = l1_cached
            if expires_at > now:
                return list(results)
            self._l1_cache.pop(l1_key, None)
        if not REDIS_CACHE_READ_ENABLED:
            return None

        cached_memories = await self._read_retrieval_cache(user_id, cache_context)
        if not cached_memories:
            return None

        results = self._filter_current_results(
            [self._memory_result_from_cache(item) for item in cached_memories]
        )
        self._set_l1_cache(l1_key, results)
        return results

    async def _cache_results(
        self,
        *,
        user_id: str,
        results: list[MemoryResult],
        cache_context: str,
    ) -> None:
        payload = []
        for result in results:
            payload.append(asdict(result))
        self._set_l1_cache(self._l1_cache_key(user_id, cache_context), results)
        self._schedule_cache_write(user_id, cache_context, payload)

    def _schedule_cache_write(
        self,
        user_id: str,
        cache_context: str,
        payload: list[dict[str, Any]],
    ) -> None:
        try:
            asyncio.create_task(self._write_retrieval_cache(user_id, cache_context, payload))
        except RuntimeError:
            return

    async def _read_retrieval_cache(
        self,
        user_id: str,
        cache_context: str,
    ) -> list[dict[str, Any]] | None:
        getter = getattr(self.cache_service, "get_retrieval_results", None)
        if callable(getter):
            value = getter(user_id, cache_context)
            if inspect.isawaitable(value):
                try:
                    cached_memories = await asyncio.wait_for(
                        value,
                        timeout=REDIS_CACHE_READ_TIMEOUT_SECONDS,
                    )
                except TimeoutError:
                    return None
                if isinstance(cached_memories, list) or cached_memories is None:
                    return cached_memories

        cached_memories = await self.cache_service.get_hot_memories(user_id)
        if not cached_memories:
            return None
        cached_context = str(cached_memories[0].get("_cache_context", "")) if cached_memories else ""
        if cached_context != cache_context:
            return None
        return cached_memories

    async def _write_retrieval_cache(
        self,
        user_id: str,
        cache_context: str,
        payload: list[dict[str, Any]],
    ) -> None:
        if not REDIS_CACHE_WRITE_ENABLED:
            return
        setter = getattr(self.cache_service, "set_retrieval_results", None)
        if callable(setter):
            value = setter(user_id, cache_context, payload, ttl=CACHE_TTL_SECONDS)
            if inspect.isawaitable(value):
                await value
                return

        legacy_payload = []
        for item in payload:
            legacy_item = dict(item)
            legacy_item["_cache_context"] = cache_context
            legacy_payload.append(legacy_item)
        await self.cache_service.set_hot_memories(user_id, legacy_payload, ttl=CACHE_TTL_SECONDS)

    @classmethod
    def _set_l1_cache(cls, key: str, results: list[MemoryResult]) -> None:
        if L1_CACHE_TTL_SECONDS <= 0:
            return
        if len(cls._l1_cache) > 1024:
            cls._l1_cache.clear()
        cls._l1_cache[key] = (datetime.now(UTC).timestamp() + L1_CACHE_TTL_SECONDS, list(results))

    @staticmethod
    def _l1_cache_key(user_id: str, cache_context: str) -> str:
        return f"{user_id}|{cache_context}"

    async def _get_hot_tier_results(
        self,
        *,
        proxy_user_id: str | None,
        categories: list[str],
        agent_id: str | None,
        created_after: datetime | None,
    ) -> list[MemoryResult]:
        if proxy_user_id is None:
            return []
        cache_key = self._hot_tier_cache_key(proxy_user_id, categories, agent_id, created_after)
        cached = self._hot_tier_cache.get(cache_key)
        now = datetime.now(UTC).timestamp()
        if cached is not None:
            expires_at, results = cached
            if expires_at > now:
                return list(results)
            self._hot_tier_cache.pop(cache_key, None)

        try:
            cached_memories = await self.cache_service.get_hot_tier_memories(proxy_user_id)
        except Exception:
            return []

        results: list[MemoryResult] = []
        for item in cached_memories:
            if not self._payload_is_current(item):
                continue
            category = str(item.get("category", ""))
            if categories and category not in categories:
                continue
            if agent_id is not None and item.get("agent_id") not in {None, agent_id}:
                continue
            if created_after is not None:
                created_at = self._parse_datetime(item.get("created_at"))
                if created_at is None or created_at < created_after:
                    continue
            try:
                results.append(self._memory_result_from_cache(item))
            except Exception:
                continue
        sorted_results = sorted(results, key=lambda item: item.final_score, reverse=True)
        self._set_hot_tier_cache(cache_key, sorted_results)
        return sorted_results

    @classmethod
    def _set_hot_tier_cache(cls, key: str, results: list[MemoryResult]) -> None:
        if HOT_TIER_CACHE_TTL_SECONDS <= 0:
            return
        if len(cls._hot_tier_cache) > 4096:
            cls._hot_tier_cache.clear()
        cls._hot_tier_cache[key] = (
            datetime.now(UTC).timestamp() + HOT_TIER_CACHE_TTL_SECONDS,
            list(results),
        )

    @staticmethod
    def _hot_tier_cache_key(
        proxy_user_id: str,
        categories: list[str],
        agent_id: str | None,
        created_after: datetime | None,
    ) -> str:
        created_after_part = created_after.isoformat() if created_after else ""
        return f"{proxy_user_id}|{','.join(sorted(categories))}|{agent_id or ''}|{created_after_part}"

    @staticmethod
    def _merge_hot_tier_results(
        hot_results: list[MemoryResult],
        other_results: list[MemoryResult],
        limit: int,
    ) -> list[MemoryResult]:
        merged: list[MemoryResult] = []
        seen_ids: set[str] = set()
        for result in [*hot_results, *other_results]:
            if result.id in seen_ids:
                continue
            merged.append(result)
            seen_ids.add(result.id)
            if len(merged) >= limit:
                break
        return merged

    async def _count_proxy_user_memories(self, proxy_user_id: str) -> int:
        cache_key = f"proxy:{proxy_user_id}"
        cached = self._memory_count_cache.get(cache_key)
        now = datetime.now(UTC).timestamp()
        if cached is not None:
            expires_at, count = cached
            if expires_at > now:
                return count
            self._memory_count_cache.pop(cache_key, None)

        query = select(func.count(Memory.id)).where(
            Memory.proxy_user_id == self._as_uuid(proxy_user_id),
            Memory.is_archived.is_(False),
        )
        result = await self.session.execute(query)
        count = int(result.scalar_one() or 0)
        self._set_memory_count_cache(cache_key, count)
        return count

    async def _count_user_memories(self, user_id: str) -> int:
        cache_key = f"user:{user_id}"
        cached = self._memory_count_cache.get(cache_key)
        now = datetime.now(UTC).timestamp()
        if cached is not None:
            expires_at, count = cached
            if expires_at > now:
                return count
            self._memory_count_cache.pop(cache_key, None)

        query = select(func.count(Memory.id)).where(
            Memory.user_id == self._as_uuid(user_id),
            Memory.is_archived.is_(False),
        )
        result = await self.session.execute(query)
        count = int(result.scalar_one() or 0)
        self._set_memory_count_cache(cache_key, count)
        return count

    @classmethod
    def _set_memory_count_cache(cls, key: str, count: int) -> None:
        if MEMORY_COUNT_CACHE_TTL_SECONDS <= 0:
            return
        if len(cls._memory_count_cache) > 4096:
            cls._memory_count_cache.clear()
        cls._memory_count_cache[key] = (datetime.now(UTC).timestamp() + MEMORY_COUNT_CACHE_TTL_SECONDS, count)

    async def _retrieve_as_of_semantic(
        self,
        *,
        query: str,
        tenant_id: str | None,
        proxy_user_id: str | None,
        user_id: str | None,
        as_of: datetime,
        limit: int,
        categories: list[str],
        agent_id: str | None,
        created_after: datetime | None,
    ) -> list[MemoryResult]:
        """Rank retained active/superseded vectors, with PostgreSQL as authority."""
        if as_of.tzinfo is None:
            raise ValueError("as_of must include a timezone")
        try:
            model_ids = await self._candidate_embedding_model_ids(
                proxy_user_id=proxy_user_id, user_id=user_id
            )
            points: list[Any] = []
            for model_id in model_ids:
                embedding = await self._embed_query(query, model_id=model_id)
                search_limit = min(max(limit * OVERFETCH_MULTIPLIER, limit), 50)
                if proxy_user_id is not None:
                    points.extend(await self._search_qdrant(
                        query_embedding=embedding.vector, tenant_id=tenant_id,
                        proxy_user_id=proxy_user_id, limit=search_limit,
                        categories=categories, agent_id=agent_id,
                        collection_name=embedding.qdrant_collection,
                        created_after=created_after, include_archived=True,
                    ))
                elif user_id is not None:
                    points.extend(await self._search_qdrant_legacy(
                        query_embedding=embedding.vector, user_id=user_id,
                        limit=search_limit, categories=categories, agent_id=agent_id,
                        collection_name=embedding.qdrant_collection,
                        created_after=created_after, include_archived=True,
                    ))
            points = [
                point for point in points
                if self._passes_semantic_floor(point)
                and self._payload_is_valid_at(
                    getattr(point, "payload", {}) or {}, as_of
                )
            ]
            if points:
                memories = await self._fetch_historical_memories_by_ids(
                    memory_ids=[self._point_memory_id(point) for point in points],
                    proxy_user_id=proxy_user_id, user_id=user_id, as_of=as_of,
                    categories=categories, agent_id=agent_id, created_after=created_after,
                )
                results: list[MemoryResult] = []
                for point in points:
                    memory_id = self._point_memory_id(point)
                    memory = memories.get(str(memory_id)) if memory_id else None
                    if memory is None:
                        continue
                    results.append(self._memory_to_result(
                        memory, semantic_score=float(getattr(point, "score", 0.0) or 0.0)
                    ))
                if results:
                    return sorted(
                        self._deduplicate_results(results),
                        key=lambda item: item.final_score, reverse=True,
                    )[:limit]
        except Exception as exc:
            logger.warning("historical semantic retrieval failed; using PostgreSQL: %s", exc)

        return await self._retrieve_as_of_memories(
            proxy_user_id=proxy_user_id, user_id=user_id, as_of=as_of, limit=limit,
            categories=categories, agent_id=agent_id, created_after=created_after,
        )

    async def _fetch_historical_memories_by_ids(
        self, *, memory_ids: Iterable[str | None], proxy_user_id: str | None,
        user_id: str | None, as_of: datetime, categories: list[str],
        agent_id: str | None, created_after: datetime | None,
    ) -> dict[str, Memory]:
        ids = [self._as_uuid(item) for item in memory_ids if item]
        if not ids:
            return {}
        effective_at = as_of.astimezone(UTC)
        query = select(Memory).where(Memory.id.in_(ids))
        if proxy_user_id is not None:
            query = query.where(Memory.proxy_user_id == self._as_uuid(proxy_user_id))
        elif user_id is not None:
            query = query.where(Memory.user_id == self._as_uuid(user_id))
        else:
            return {}
        query = query.where(
            or_(Memory.effective_from.is_(None), Memory.effective_from <= effective_at),
            or_(Memory.effective_until.is_(None), Memory.effective_until > effective_at),
            or_(Memory.is_archived.is_(False), Memory.effective_from.is_not(None), Memory.effective_until.is_not(None)),
        )
        if categories:
            query = query.where(Memory.category.in_(categories))
        if agent_id is not None:
            query = query.where(Memory.agent_id == self._as_uuid(agent_id))
        if created_after is not None:
            query = query.where(Memory.created_at >= created_after)
        rows = list((await self.session.execute(query)).scalars().all())
        return {str(memory.id): memory for memory in rows}

    async def _retrieve_as_of_memories(
        self,
        *,
        proxy_user_id: str | None,
        user_id: str | None,
        as_of: datetime,
        limit: int,
        categories: list[str],
        agent_id: str | None,
        created_after: datetime | None,
    ) -> list[MemoryResult]:
        if as_of.tzinfo is None:
            raise ValueError("as_of must include a timezone")
        effective_at = as_of.astimezone(UTC)

        if proxy_user_id is not None:
            query = select(Memory).where(
                Memory.proxy_user_id == self._as_uuid(proxy_user_id)
            )
        elif user_id is not None:
            query = select(Memory).where(Memory.user_id == self._as_uuid(user_id))
        else:
            return []

        query = query.where(
            and_(
                or_(
                    Memory.effective_from.is_(None),
                    Memory.effective_from <= effective_at,
                ),
                or_(
                    Memory.effective_until.is_(None),
                    Memory.effective_until > effective_at,
                ),
                or_(
                    Memory.is_archived.is_(False),
                    Memory.effective_from.is_not(None),
                    Memory.effective_until.is_not(None),
                ),
            )
        )
        if categories:
            query = query.where(Memory.category.in_(categories))
        if agent_id is not None:
            query = query.where(Memory.agent_id == self._as_uuid(agent_id))
        if created_after is not None:
            query = query.where(Memory.created_at >= created_after)

        query = query.order_by(
            Memory.importance_score.desc(),
            Memory.effective_from.desc().nullslast(),
            Memory.last_accessed_at.desc(),
        ).limit(limit)
        result = await self.session.execute(query)
        return [
            self._memory_to_result(memory, semantic_score=0.0)
            for memory in result.scalars().all()
        ]

    async def _retrieve_postgres_fallback(
        self,
        *,
        proxy_user_id: str | None,
        user_id: str | None,
        limit: int,
        categories: list[str],
        agent_id: str | None,
        created_after: datetime | None,
    ) -> list[MemoryResult]:
        if limit <= 0:
            return []
        if proxy_user_id is None and user_id is None:
            return []

        if proxy_user_id is not None:
            query = select(Memory).where(Memory.proxy_user_id == self._as_uuid(proxy_user_id))
        else:
            query = select(Memory).where(Memory.user_id == self._as_uuid(user_id or ""))

        query = query.where(Memory.is_archived.is_(False))
        if categories:
            query = query.where(Memory.category.in_([category for category in categories]))
        if agent_id is not None:
            query = query.where(Memory.agent_id == self._as_uuid(agent_id))
        if created_after is not None:
            query = query.where(Memory.created_at >= created_after)

        query = query.order_by(
            Memory.importance_score.desc(),
            Memory.last_accessed_at.desc(),
        ).limit(limit)
        result = await self.session.execute(query)
        return self._filter_current_results(
            [self._memory_to_result(memory, semantic_score=0.0) for memory in result.scalars().all()]
        )

    async def _retrieve_cold_start_memories(
        self,
        *,
        proxy_user_id: str,
        limit: int,
        categories: list[str],
        agent_id: str | None,
        created_after: datetime | None,
    ) -> list[MemoryResult]:
        memories = await self._fetch_cold_start_memories(
            proxy_user_id=proxy_user_id,
            categories=categories,
            agent_id=agent_id,
            created_after=created_after,
        )
        results = [
            MemoryResult(
                id=str(memory.id),
                content=memory.content,
                category=memory.category.value,
                importance_score=float(memory.importance_score),
                confidence_score=float(memory.confidence_score),
                semantic_score=1.0,
                recency_score=self._recency_score(memory.last_accessed_at),
                final_score=round(
                    (self.SEMANTIC_WEIGHT * 1.0)
                    + (self.IMPORTANCE_WEIGHT * (float(memory.importance_score) / 10.0))
                    + (self.RECENCY_WEIGHT * self._recency_score(memory.last_accessed_at)),
                    6,
                ),
                agent_id=str(memory.agent_id) if memory.agent_id else None,
                previous_version_id=(
                    str(memory.previous_version_id) if memory.previous_version_id else None
                ),
                last_accessed_at=(
                    memory.last_accessed_at.isoformat() if memory.last_accessed_at else None
                ),
                created_at=memory.created_at.isoformat() if memory.created_at else None,
                source_event_id=str(memory.source_event_id) if memory.source_event_id else None,
                provenance=(memory.metadata_json or {}).get("provenance"),
                effective_from=memory.effective_from.isoformat() if memory.effective_from else None,
                effective_until=memory.effective_until.isoformat() if memory.effective_until else None,
            )
            for memory in memories
            if self._memory_is_current(memory)
        ]
        deduplicated_results = self._deduplicate_results(results)
        return sorted(deduplicated_results, key=lambda item: item.final_score, reverse=True)[:limit]

    async def _retrieve_cold_start_memories_for_user(
        self,
        *,
        user_id: str,
        limit: int,
        categories: list[str],
        agent_id: str | None,
        created_after: datetime | None,
    ) -> list[MemoryResult]:
        memories = await self._fetch_cold_start_memories_for_user(
            user_id=user_id,
            categories=categories,
            agent_id=agent_id,
            created_after=created_after,
        )
        results = [
            MemoryResult(
                id=str(memory.id),
                content=memory.content,
                category=memory.category.value,
                importance_score=float(memory.importance_score),
                confidence_score=float(memory.confidence_score),
                semantic_score=1.0,
                recency_score=self._recency_score(memory.last_accessed_at),
                final_score=round(
                    (self.SEMANTIC_WEIGHT * 1.0)
                    + (self.IMPORTANCE_WEIGHT * (float(memory.importance_score) / 10.0))
                    + (self.RECENCY_WEIGHT * self._recency_score(memory.last_accessed_at)),
                    6,
                ),
                agent_id=str(memory.agent_id) if memory.agent_id else None,
                previous_version_id=str(memory.previous_version_id) if memory.previous_version_id else None,
                last_accessed_at=memory.last_accessed_at.isoformat() if memory.last_accessed_at else None,
                created_at=memory.created_at.isoformat() if memory.created_at else None,
                source_event_id=str(memory.source_event_id) if memory.source_event_id else None,
                provenance=(memory.metadata_json or {}).get("provenance"),
                effective_from=memory.effective_from.isoformat() if memory.effective_from else None,
                effective_until=memory.effective_until.isoformat() if memory.effective_until else None,
            )
            for memory in memories
            if self._memory_is_current(memory)
        ]
        deduplicated_results = self._deduplicate_results(results)
        return sorted(deduplicated_results, key=lambda item: item.final_score, reverse=True)[:limit]

    async def _fetch_cold_start_memories(
        self,
        *,
        proxy_user_id: str,
        categories: list[str],
        agent_id: str | None,
        created_after: datetime | None = None,
    ) -> list[Memory]:
        query = select(Memory).where(
            Memory.proxy_user_id == self._as_uuid(proxy_user_id),
            Memory.is_archived.is_(False),
        )
        if categories:
            query = query.where(Memory.category.in_([category for category in categories]))
        if agent_id is not None:
            query = query.where(Memory.agent_id == self._as_uuid(agent_id))
        if created_after is not None:
            query = query.where(Memory.created_at >= created_after)

        result = await self.session.execute(query)
        return list(result.scalars().all())

    async def _fetch_cold_start_memories_for_user(
        self,
        *,
        user_id: str,
        categories: list[str],
        agent_id: str | None,
        created_after: datetime | None = None,
    ) -> list[Memory]:
        query = select(Memory).where(
            Memory.user_id == self._as_uuid(user_id),
            Memory.is_archived.is_(False),
        )
        if categories:
            query = query.where(Memory.category.in_([category for category in categories]))
        if agent_id is not None:
            query = query.where(Memory.agent_id == self._as_uuid(agent_id))
        if created_after is not None:
            query = query.where(Memory.created_at >= created_after)
        result = await self.session.execute(query)
        return list(result.scalars().all())

    async def _fetch_memories_by_ids(
        self,
        *,
        memory_ids: Iterable[str | None],
        proxy_user_id: str,
        categories: list[str],
        agent_id: str | None,
        created_after: datetime | None = None,
    ) -> dict[str, Memory]:
        normalized_ids = [self._as_uuid(memory_id) for memory_id in memory_ids if memory_id]
        if not normalized_ids:
            return {}

        query = select(Memory).where(
            Memory.id.in_(normalized_ids),
            Memory.proxy_user_id == self._as_uuid(proxy_user_id),
            Memory.is_archived.is_(False),
        )
        if categories:
            query = query.where(Memory.category.in_([category for category in categories]))
        if agent_id is not None:
            query = query.where(Memory.agent_id == self._as_uuid(agent_id))
        if created_after is not None:
            query = query.where(Memory.created_at >= created_after)

        result = await self.session.execute(query)
        memories = list(result.scalars().all())
        return {str(memory.id): memory for memory in memories}

    async def _fetch_memories_by_ids_for_user(
        self,
        *,
        memory_ids: Iterable[str | None],
        user_id: str,
        categories: list[str],
        agent_id: str | None,
        created_after: datetime | None = None,
    ) -> dict[str, Memory]:
        normalized_ids = [self._as_uuid(memory_id) for memory_id in memory_ids if memory_id]
        if not normalized_ids:
            return {}
        query = select(Memory).where(
            Memory.id.in_(normalized_ids),
            Memory.user_id == self._as_uuid(user_id),
            Memory.is_archived.is_(False),
        )
        if categories:
            query = query.where(Memory.category.in_([category for category in categories]))
        if agent_id is not None:
            query = query.where(Memory.agent_id == self._as_uuid(agent_id))
        if created_after is not None:
            query = query.where(Memory.created_at >= created_after)
        result = await self.session.execute(query)
        memories = list(result.scalars().all())
        return {str(memory.id): memory for memory in memories}

    async def _embed_query(self, query: str, *, model_id: str) -> EmbeddingResult:
        return await self.embedding_service.embed(query, model_id=model_id)

    async def _search_qdrant(
        self,
        *,
        query_embedding: list[float],
        tenant_id: str | None,
        proxy_user_id: str,
        limit: int,
        categories: list[str],
        agent_id: str | None,
        collection_name: str,
        created_after: datetime | None,
        include_archived: bool = False,
    ) -> list[Any]:
        if len(categories) <= 1:
            return await self._search_memories(
                query_embedding=query_embedding,
                tenant_id=tenant_id,
                proxy_user_id=proxy_user_id,
                limit=limit,
                category_filter=categories[0] if categories else None,
                agent_id=agent_id,
                include_archived=include_archived,
                collection_name=collection_name,
                created_after=created_after,
            )

        merged_points: dict[str, Any] = {}
        for category in categories:
            for point in await self._search_memories(
                query_embedding=query_embedding,
                tenant_id=tenant_id,
                proxy_user_id=proxy_user_id,
                limit=limit,
                category_filter=category,
                agent_id=agent_id,
                include_archived=include_archived,
                collection_name=collection_name,
                created_after=created_after,
            ):
                point_id = self._point_memory_id(point)
                if point_id is None:
                    continue
                existing = merged_points.get(point_id)
                if existing is None or float(getattr(point, "score", 0.0) or 0.0) > float(
                    getattr(existing, "score", 0.0) or 0.0
                ):
                    merged_points[point_id] = point
        return sorted(
            merged_points.values(),
            key=lambda point: float(getattr(point, "score", 0.0) or 0.0),
            reverse=True,
        )[:limit]

    async def _search_qdrant_legacy(
        self,
        *,
        query_embedding: list[float],
        user_id: str,
        limit: int,
        categories: list[str],
        agent_id: str | None,
        collection_name: str,
        created_after: datetime | None,
        include_archived: bool = False,
    ) -> list[Any]:
        if len(categories) <= 1:
            return await self._search_memories(
                query_embedding=query_embedding,
                user_id=user_id,
                limit=limit,
                category_filter=categories[0] if categories else None,
                agent_id=agent_id,
                include_archived=include_archived,
                collection_name=collection_name,
                created_after=created_after,
            )

        merged_points: dict[str, Any] = {}
        for category in categories:
            for point in await self._search_memories(
                query_embedding=query_embedding,
                user_id=user_id,
                limit=limit,
                category_filter=category,
                agent_id=agent_id,
                include_archived=include_archived,
                collection_name=collection_name,
                created_after=created_after,
            ):
                point_id = self._point_memory_id(point)
                if point_id is None:
                    continue
                existing = merged_points.get(point_id)
                if existing is None or float(getattr(point, "score", 0.0) or 0.0) > float(
                    getattr(existing, "score", 0.0) or 0.0
                ):
                    merged_points[point_id] = point
        return sorted(
            merged_points.values(),
            key=lambda point: float(getattr(point, "score", 0.0) or 0.0),
            reverse=True,
        )[:limit]

    async def _search_memories(self, **kwargs: Any) -> list[Any]:
        async_method = getattr(self.qdrant_service, "search_memories_async", None)
        if callable(async_method):
            result = async_method(**kwargs)
            if inspect.isawaitable(result):
                return list(await result)

        sync_method = getattr(self.qdrant_service, "search_memories")
        return list(await asyncio.to_thread(sync_method, **kwargs))

    def _queue_access_update(self, memory_ids: list[str]) -> None:
        if not memory_ids:
            return
        try:
            update_memory_accesses.delay(memory_ids)
        except Exception:
            return

    @staticmethod
    def _cache_context(
        query: str,
        categories: list[str],
        agent_id: str | None,
        limit: int,
        time_filter_days: int | None = None,
        as_of: datetime | None = None,
    ) -> str:
        categories_part = ",".join(sorted(categories))
        agent_part = agent_id or ""
        base = f"{query.strip().lower()}|{categories_part}|{agent_part}|{limit}"
        if as_of is None:
            if time_filter_days is None:
                return base
            return f"{base}|{int(time_filter_days)}"
        time_part = "" if time_filter_days is None else str(int(time_filter_days))
        return f"{base}|{time_part}|{as_of.astimezone(UTC).isoformat()}"

    @staticmethod
    def _memory_result_from_cache(item: dict[str, Any]) -> MemoryResult:
        return MemoryResult(
            id=str(item["id"]),
            content=str(item["content"]),
            category=str(item["category"]),
            importance_score=float(item["importance_score"]),
            confidence_score=float(item["confidence_score"]),
            semantic_score=float(item["semantic_score"]),
            recency_score=float(item["recency_score"]),
            final_score=float(item["final_score"]),
            agent_id=str(item["agent_id"]) if item.get("agent_id") else None,
            previous_version_id=(
                str(item["previous_version_id"]) if item.get("previous_version_id") else None
            ),
            last_accessed_at=(
                str(item["last_accessed_at"]) if item.get("last_accessed_at") else None
            ),
            created_at=str(item["created_at"]) if item.get("created_at") else None,
            source_event_id=str(item["source_event_id"]) if item.get("source_event_id") else None,
            provenance=dict(item["provenance"]) if item.get("provenance") else None,
            effective_from=str(item["effective_from"]) if item.get("effective_from") else None,
            effective_until=str(item["effective_until"]) if item.get("effective_until") else None,
        )

    def _memory_to_result(self, memory: Memory, *, semantic_score: float) -> MemoryResult:
        recency_score = self._recency_score(memory.last_accessed_at)
        final_score = (
            (self.SEMANTIC_WEIGHT * semantic_score)
            + (self.IMPORTANCE_WEIGHT * (float(memory.importance_score) / 10.0))
            + (self.RECENCY_WEIGHT * recency_score)
        )
        return MemoryResult(
            id=str(memory.id),
            content=memory.content,
            category=memory.category.value,
            importance_score=float(memory.importance_score),
            confidence_score=float(memory.confidence_score),
            semantic_score=semantic_score,
            recency_score=recency_score,
            final_score=round(final_score, 6),
            agent_id=str(memory.agent_id) if memory.agent_id else None,
            previous_version_id=str(memory.previous_version_id) if memory.previous_version_id else None,
            last_accessed_at=memory.last_accessed_at.isoformat() if memory.last_accessed_at else None,
            created_at=memory.created_at.isoformat() if memory.created_at else None,
            source_event_id=str(memory.source_event_id) if memory.source_event_id else None,
            provenance=(memory.metadata_json or {}).get("provenance"),
            effective_from=memory.effective_from.isoformat() if memory.effective_from else None,
            effective_until=memory.effective_until.isoformat() if memory.effective_until else None,
        )

    def _results_from_qdrant_payloads(
        self, scored_points: list[Any], *, agent_id: str | None = None
    ) -> list[MemoryResult]:
        results: list[MemoryResult] = []
        for point in scored_points:
            payload = getattr(point, "payload", {}) or {}
            if agent_id is not None and str(payload.get("agent_id") or "") != str(agent_id):
                continue
            memory_id = self._point_memory_id(point)
            content = payload.get("content")
            category = payload.get("category")
            importance_score = payload.get("importance_score")
            if not memory_id or not content or not category or importance_score is None:
                return []

            semantic_score = float(getattr(point, "score", 0.0) or 0.0)
            last_accessed_at = payload.get("last_accessed_at")
            created_at = payload.get("created_at")
            recency_source = self._parse_datetime(last_accessed_at) or self._parse_datetime(created_at)
            recency_score = self._recency_score(recency_source)
            importance = float(importance_score)
            final_score = (
                (self.SEMANTIC_WEIGHT * semantic_score)
                + (self.IMPORTANCE_WEIGHT * (importance / 10.0))
                + (self.RECENCY_WEIGHT * recency_score)
            )
            results.append(
                MemoryResult(
                    id=str(memory_id),
                    content=str(content),
                    category=str(category),
                    importance_score=importance,
                    confidence_score=float(payload.get("confidence_score") or payload.get("confidence") or 1.0),
                    semantic_score=semantic_score,
                    recency_score=recency_score,
                    final_score=round(final_score, 6),
                    agent_id=str(payload["agent_id"]) if payload.get("agent_id") else None,
                    previous_version_id=(
                        str(payload["previous_version_id"]) if payload.get("previous_version_id") else None
                    ),
                    last_accessed_at=str(last_accessed_at) if last_accessed_at else None,
                    created_at=str(created_at) if created_at else None,
                    source_event_id=(
                        str(payload["source_event_id"]) if payload.get("source_event_id") else None
                    ),
                    provenance=dict(payload["provenance"]) if payload.get("provenance") else None,
                    effective_from=str(payload["effective_from"]) if payload.get("effective_from") else None,
                    effective_until=str(payload["effective_until"]) if payload.get("effective_until") else None,
                )
            )
        return results

    @classmethod
    def _deduplicate_results(cls, results: list[MemoryResult]) -> list[MemoryResult]:
        deduplicated: list[MemoryResult] = []
        for candidate in sorted(results, key=lambda item: item.final_score, reverse=True):
            if any(cls._content_similarity(candidate.content, item.content) > DEDUPLICATION_THRESHOLD for item in deduplicated):
                continue
            deduplicated.append(candidate)
        return deduplicated

    @staticmethod
    def _content_similarity(left: str, right: str) -> float:
        normalized_left = " ".join(left.lower().split())
        normalized_right = " ".join(right.lower().split())
        if normalized_left == normalized_right:
            return 1.0
        left_tokens = set(normalized_left.rstrip(".,;:!?").split())
        right_tokens = set(normalized_right.rstrip(".,;:!?").split())
        if not left_tokens or not right_tokens:
            return 0.0
        return len(left_tokens & right_tokens) / len(left_tokens | right_tokens)

    @staticmethod
    def _recency_score(last_accessed_at: datetime | None) -> float:
        if last_accessed_at is None:
            return 0.0

        reference = datetime.now(UTC)
        if last_accessed_at.tzinfo is None:
            last_accessed_at = last_accessed_at.replace(tzinfo=UTC)

        age_days = (reference - last_accessed_at).days
        if age_days <= 7:
            return 1.0
        if age_days <= 30:
            return 0.5
        return 0.0

    @staticmethod
    def _created_after(time_filter_days: int | None) -> datetime | None:
        if time_filter_days is None:
            return None
        return datetime.now(UTC) - timedelta(days=max(0, int(time_filter_days)))

    @classmethod
    def _filter_current_results(
        cls, results: list[MemoryResult], *, now: datetime | None = None
    ) -> list[MemoryResult]:
        reference = now or datetime.now(UTC)
        return [
            result for result in results
            if cls._is_valid_at(result.effective_from, result.effective_until, reference)
        ]

    @classmethod
    def _payload_is_valid_at(
        cls, payload: dict[str, Any], reference: datetime
    ) -> bool:
        return cls._is_valid_at(
            payload.get("effective_from"), payload.get("effective_until"), reference
        )

    @classmethod
    def _payload_is_current(
        cls, payload: dict[str, Any], *, now: datetime | None = None
    ) -> bool:
        reference = now or datetime.now(UTC)
        return cls._is_valid_at(
            payload.get("effective_from"), payload.get("effective_until"), reference
        )

    @classmethod
    def _memory_is_current(
        cls, memory: Memory, *, now: datetime | None = None
    ) -> bool:
        reference = now or datetime.now(UTC)
        return cls._is_valid_at(
            getattr(memory, "effective_from", None),
            getattr(memory, "effective_until", None),
            reference,
        )

    @classmethod
    def _is_valid_at(
        cls, effective_from: Any, effective_until: Any, reference: datetime
    ) -> bool:
        starts_at = cls._parse_datetime(effective_from)
        ends_at = cls._parse_datetime(effective_until)
        if reference.tzinfo is None:
            reference = reference.replace(tzinfo=UTC)
        else:
            reference = reference.astimezone(UTC)
        return (starts_at is None or starts_at <= reference) and (
            ends_at is None or ends_at > reference
        )

    @staticmethod
    def _parse_datetime(value: Any) -> datetime | None:
        if value is None:
            return None
        if isinstance(value, datetime):
            parsed = value
        else:
            try:
                parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
            except ValueError:
                return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return parsed

    @staticmethod
    def _passes_semantic_floor(point: Any) -> bool:
        return float(getattr(point, "score", 0.0) or 0.0) >= MIN_SEMANTIC_SCORE

    @staticmethod
    def _point_memory_id(point: Any) -> str | None:
        payload = getattr(point, "payload", {}) or {}
        return payload.get("memory_id") or (str(point.id) if getattr(point, "id", None) is not None else None)

    @staticmethod
    def _as_uuid(value: str | uuid.UUID) -> uuid.UUID:
        return value if isinstance(value, uuid.UUID) else uuid.UUID(str(value))

    async def _candidate_embedding_model_ids(
        self,
        *,
        proxy_user_id: str | None,
        user_id: str | None,
    ) -> list[str]:
        cache_key = f"proxy:{proxy_user_id}" if proxy_user_id is not None else f"user:{user_id}"
        cached = self._model_id_cache.get(cache_key)
        now = datetime.now(UTC).timestamp()
        if cached is not None:
            expires_at, model_ids = cached
            if expires_at > now:
                return list(model_ids)
            self._model_id_cache.pop(cache_key, None)

        if proxy_user_id is not None:
            stmt = (
                select(Memory.embedding_model_id)
                .where(
                    Memory.proxy_user_id == self._as_uuid(proxy_user_id),
                    Memory.is_archived.is_(False),
                )
                .distinct()
            )
        elif user_id is not None:
            stmt = (
                select(Memory.embedding_model_id)
                .where(
                    Memory.user_id == self._as_uuid(user_id),
                    Memory.is_archived.is_(False),
                )
                .distinct()
            )
        else:
            active_model = await self.embedding_service.get_active_model()
            return [active_model.id]

        result = await self.session.execute(stmt)
        model_ids = [str(value) for value in result.scalars().all() if value]
        if model_ids:
            self._set_model_id_cache(cache_key, model_ids)
            return model_ids

        active_model = await self.embedding_service.get_active_model()
        model_ids = [active_model.id]
        self._set_model_id_cache(cache_key, model_ids)
        return model_ids

    @classmethod
    def _set_model_id_cache(cls, key: str, model_ids: list[str]) -> None:
        if MODEL_ID_CACHE_TTL_SECONDS <= 0:
            return
        if len(cls._model_id_cache) > 4096:
            cls._model_id_cache.clear()
        cls._model_id_cache[key] = (
            datetime.now(UTC).timestamp() + MODEL_ID_CACHE_TTL_SECONDS,
            list(model_ids),
        )
