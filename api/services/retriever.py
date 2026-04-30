from __future__ import annotations

import os
import uuid
from dataclasses import asdict
from dataclasses import dataclass
from datetime import UTC
from datetime import datetime
from difflib import SequenceMatcher
import logging
from types import SimpleNamespace
from typing import Any
from typing import Iterable

from sqlalchemy import func
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from api.db.cache import CacheService
from api.db.models import Memory
from api.db.models import EmbeddingModel
from api.db.models import QuotaMode
from api.db.vector_store import QdrantService
from api.infra.circuit_breaker_registry import CircuitBreakerRegistry
from api.services.embedding_service import EmbeddingResult
from api.services.embedding_service import EmbeddingService
from api.services.proxy_user_service import ProxyUserService
from api.services.quota_manager import QuotaManager
from api.tasks.retrieval_tasks import update_memory_accesses


CACHE_TTL_SECONDS = 300
COLD_START_THRESHOLD = 5
DEDUPLICATION_THRESHOLD = 0.95


logger = logging.getLogger(__name__)


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


class RetrieverService:
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
        self.region_id = region_id
        self.embedding_service = embedding_service or EmbeddingService(
            async_session=session,
            gemini_client=client,
        )

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
    ) -> list[MemoryResult]:
        identity = external_user_id or user_id
        if not identity:
            raise ValueError("retrieve requires external_user_id or user_id.")
        normalized_categories = [category.lower() for category in categories or []]
        cache_context = self._cache_context(query, normalized_categories, agent_id, limit)

        quota_mode = await self._resolve_quota_mode(tenant_id)
        self.last_quota_mode = quota_mode.value

        if quota_mode in {QuotaMode.blocked, QuotaMode.passthrough}:
            self.last_cache_hit = False
            return []

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

        hot_tier_results = await self._get_hot_tier_results(
            proxy_user_id=str(proxy_user.id) if proxy_user is not None else None,
            categories=normalized_categories,
            agent_id=agent_id,
        )

        cached_results = await self._get_cached_results(user_id=cache_identity, cache_context=cache_context)
        if cached_results is not None:
            self.last_cache_hit = True
            merged_results = self._merge_hot_tier_results(hot_tier_results, cached_results, limit)
            self._queue_access_update([result.id for result in merged_results])
            return merged_results
        self.last_cache_hit = False

        if quota_mode == QuotaMode.degraded_retrieve:
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
                )
            else:
                cold_start_results = await self._retrieve_cold_start_memories_for_user(
                    user_id=scoped_identity,
                    limit=max(0, limit - len(hot_tier_results)),
                    categories=normalized_categories,
                    agent_id=agent_id,
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
            logger.warning(
                "Qdrant circuit already open; skipping embeddings and vector search. tenant_id=%s proxy_user_id=%s",
                tenant_id,
                scoped_identity if proxy_user is not None else None,
            )
            self._queue_access_update([result.id for result in hot_tier_results])
            return hot_tier_results[:limit]

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
                "Retriever embedding failed; returning empty results. tenant_id=%s proxy_user_id=%s error=%s",
                tenant_id,
                scoped_identity if proxy_user is not None else None,
                exc,
            )
            self._queue_access_update([result.id for result in hot_tier_results])
            return hot_tier_results[:limit]
        if proxy_user is not None:
            scored_points = []
            for query_embedding in query_embeddings:
                scored_points.extend(
                    self._search_qdrant(
                        query_embedding=query_embedding.vector,
                        tenant_id=tenant_id,
                        proxy_user_id=scoped_identity,
                        limit=max(remaining_limit * 4, remaining_limit),
                        categories=normalized_categories,
                        collection_name=query_embedding.qdrant_collection,
                    )
                )
        else:
            scored_points = []
            for query_embedding in query_embeddings:
                scored_points.extend(
                    self._search_qdrant_legacy(
                        query_embedding=query_embedding.vector,
                        user_id=scoped_identity,
                        limit=max(remaining_limit * 4, remaining_limit),
                        categories=normalized_categories,
                        collection_name=query_embedding.qdrant_collection,
                    )
                )

        if not scored_points:
            self._queue_access_update([result.id for result in hot_tier_results])
            return hot_tier_results[:limit]

        top_memory_ids = [self._point_memory_id(point) for point in scored_points]
        if proxy_user is not None:
            memories_by_id = await self._fetch_memories_by_ids(
                memory_ids=top_memory_ids,
                proxy_user_id=scoped_identity,
                categories=normalized_categories,
                agent_id=agent_id,
            )
        else:
            memories_by_id = await self._fetch_memories_by_ids_for_user(
                memory_ids=top_memory_ids,
                user_id=scoped_identity,
                categories=normalized_categories,
                agent_id=agent_id,
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
                (0.60 * semantic_score)
                + (0.25 * (float(memory.importance_score) / 10.0))
                + (0.15 * recency_score)
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

    async def _get_cached_results(
        self,
        *,
        user_id: str,
        cache_context: str,
    ) -> list[MemoryResult] | None:
        cached_memories = await self.cache_service.get_hot_memories(user_id)
        if not cached_memories:
            return None

        cached_context = str(cached_memories[0].get("_cache_context", "")) if cached_memories else ""
        if cached_context != cache_context:
            return None

        return [self._memory_result_from_cache(item) for item in cached_memories]

    async def _cache_results(
        self,
        *,
        user_id: str,
        results: list[MemoryResult],
        cache_context: str,
    ) -> None:
        payload = []
        for result in results:
            item = asdict(result)
            item["_cache_context"] = cache_context
            payload.append(item)
        await self.cache_service.set_hot_memories(user_id, payload, ttl=CACHE_TTL_SECONDS)

    async def _get_hot_tier_results(
        self,
        *,
        proxy_user_id: str | None,
        categories: list[str],
        agent_id: str | None,
    ) -> list[MemoryResult]:
        if proxy_user_id is None:
            return []
        try:
            cached_memories = await self.cache_service.get_hot_tier_memories(proxy_user_id)
        except Exception:
            return []

        results: list[MemoryResult] = []
        for item in cached_memories:
            category = str(item.get("category", ""))
            if categories and category not in categories:
                continue
            if agent_id is not None and item.get("agent_id") not in {None, agent_id}:
                continue
            try:
                results.append(self._memory_result_from_cache(item))
            except Exception:
                continue
        return sorted(results, key=lambda item: item.final_score, reverse=True)

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
        query = select(func.count(Memory.id)).where(
            Memory.proxy_user_id == self._as_uuid(proxy_user_id),
            Memory.is_archived.is_(False),
        )
        result = await self.session.execute(query)
        return int(result.scalar_one() or 0)

    async def _count_user_memories(self, user_id: str) -> int:
        query = select(func.count(Memory.id)).where(
            Memory.user_id == self._as_uuid(user_id),
            Memory.is_archived.is_(False),
        )
        result = await self.session.execute(query)
        return int(result.scalar_one() or 0)

    async def _retrieve_cold_start_memories(
        self,
        *,
        proxy_user_id: str,
        limit: int,
        categories: list[str],
        agent_id: str | None,
    ) -> list[MemoryResult]:
        memories = await self._fetch_cold_start_memories(
            proxy_user_id=proxy_user_id,
            categories=categories,
            agent_id=agent_id,
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
                    (0.60 * 1.0)
                    + (0.25 * (float(memory.importance_score) / 10.0))
                    + (0.15 * self._recency_score(memory.last_accessed_at)),
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
            )
            for memory in memories
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
    ) -> list[MemoryResult]:
        memories = await self._fetch_cold_start_memories_for_user(
            user_id=user_id,
            categories=categories,
            agent_id=agent_id,
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
                    (0.60 * 1.0)
                    + (0.25 * (float(memory.importance_score) / 10.0))
                    + (0.15 * self._recency_score(memory.last_accessed_at)),
                    6,
                ),
                agent_id=str(memory.agent_id) if memory.agent_id else None,
                previous_version_id=str(memory.previous_version_id) if memory.previous_version_id else None,
                last_accessed_at=memory.last_accessed_at.isoformat() if memory.last_accessed_at else None,
                created_at=memory.created_at.isoformat() if memory.created_at else None,
            )
            for memory in memories
        ]
        deduplicated_results = self._deduplicate_results(results)
        return sorted(deduplicated_results, key=lambda item: item.final_score, reverse=True)[:limit]

    async def _fetch_cold_start_memories(
        self,
        *,
        proxy_user_id: str,
        categories: list[str],
        agent_id: str | None,
    ) -> list[Memory]:
        query = select(Memory).where(
            Memory.proxy_user_id == self._as_uuid(proxy_user_id),
            Memory.is_archived.is_(False),
        )
        if categories:
            query = query.where(Memory.category.in_([category for category in categories]))
        if agent_id is not None:
            query = query.where(Memory.agent_id == self._as_uuid(agent_id))

        result = await self.session.execute(query)
        return list(result.scalars().all())

    async def _fetch_cold_start_memories_for_user(
        self,
        *,
        user_id: str,
        categories: list[str],
        agent_id: str | None,
    ) -> list[Memory]:
        query = select(Memory).where(
            Memory.user_id == self._as_uuid(user_id),
            Memory.is_archived.is_(False),
        )
        if categories:
            query = query.where(Memory.category.in_([category for category in categories]))
        if agent_id is not None:
            query = query.where(Memory.agent_id == self._as_uuid(agent_id))
        result = await self.session.execute(query)
        return list(result.scalars().all())

    async def _fetch_memories_by_ids(
        self,
        *,
        memory_ids: Iterable[str | None],
        proxy_user_id: str,
        categories: list[str],
        agent_id: str | None,
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
        result = await self.session.execute(query)
        memories = list(result.scalars().all())
        return {str(memory.id): memory for memory in memories}

    async def _embed_query(self, query: str, *, model_id: str) -> EmbeddingResult:
        return await self.embedding_service.embed(query, model_id=model_id)

    def _search_qdrant(
        self,
        *,
        query_embedding: list[float],
        tenant_id: str | None,
        proxy_user_id: str,
        limit: int,
        categories: list[str],
        collection_name: str,
    ) -> list[Any]:
        if len(categories) <= 1:
            return self.qdrant_service.search_memories(
                query_embedding=query_embedding,
                tenant_id=tenant_id,
                proxy_user_id=proxy_user_id,
                limit=limit,
                category_filter=categories[0] if categories else None,
                include_archived=False,
                collection_name=collection_name,
            )

        merged_points: dict[str, Any] = {}
        for category in categories:
            for point in self.qdrant_service.search_memories(
                query_embedding=query_embedding,
                tenant_id=tenant_id,
                proxy_user_id=proxy_user_id,
                limit=limit,
                category_filter=category,
                include_archived=False,
                collection_name=collection_name,
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

    def _search_qdrant_legacy(
        self,
        *,
        query_embedding: list[float],
        user_id: str,
        limit: int,
        categories: list[str],
        collection_name: str,
    ) -> list[Any]:
        if len(categories) <= 1:
            return self.qdrant_service.search_memories(
                query_embedding=query_embedding,
                user_id=user_id,
                limit=limit,
                category_filter=categories[0] if categories else None,
                include_archived=False,
                collection_name=collection_name,
            )

        merged_points: dict[str, Any] = {}
        for category in categories:
            for point in self.qdrant_service.search_memories(
                query_embedding=query_embedding,
                user_id=user_id,
                limit=limit,
                category_filter=category,
                include_archived=False,
                collection_name=collection_name,
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
    ) -> str:
        categories_part = ",".join(sorted(categories))
        agent_part = agent_id or ""
        return f"{query.strip().lower()}|{categories_part}|{agent_part}|{limit}"

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
        )

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
        return SequenceMatcher(None, normalized_left, normalized_right).ratio()

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
            return model_ids

        active_model = await self.embedding_service.get_active_model()
        return [active_model.id]
