from __future__ import annotations

import asyncio
import inspect
import logging
import os
import uuid
from collections.abc import Iterable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from itertools import islice
from types import SimpleNamespace
from typing import Any

from sqlalchemy import and_, func, literal_column, or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from api.db.cache import CacheService
from api.db.models import (
    ExtractionJob,
    ExtractionJobStatus,
    Memory,
    ProxyUser,
    QuotaMode,
)
from api.db.vector_store import QdrantService
from api.errors import APIError
from api.infra.circuit_breaker_registry import CircuitBreakerRegistry
from api.schemas.preferences import (
    PreferenceApplicability,
    PreferenceReadContext,
    PreferenceReadDecision,
    PreferenceScopeRequest,
)
from api.schemas.responses import MemoryVisibility
from api.services.embedding_service import EmbeddingResult, EmbeddingService
from api.services.preference_claim_service import (
    held_choice,
    legacy_memory_predicate,
    preference_declaration,
    preference_fingerprint,
)
from api.services.preference_discovery import (
    discover_preferences,
    discovery_enabled,
    preference_rows,
)
from api.services.preference_intent_admission_service import (
    PreferenceAdmissionUnavailable,
)
from api.services.proxy_user_service import ProxyUserService
from api.services.quota_manager import QuotaManager
from api.services.runtime_fact_visibility import unresolved_runtime_predicate
from api.services.runtime_preference_routing import target_release_enabled
from api.services.runtime_withholding import RuntimeHold, read_runtime_hold
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

    @staticmethod
    def _runtime_job_predicate():
        # Pre-reservation captures remain guarded across upgrade/rollback.
        return or_(ExtractionJob.runtime_route.is_not(None),
                   ExtractionJob.preference_intent_admission.op("?")(literal_column("'runtime_source_sha256'")))

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
        self.last_preference_decisions: list[PreferenceReadDecision] = []
        self.last_quota_mode = QuotaMode.full.value
        self.last_is_degraded = False
        self.last_memory_visibility: MemoryVisibility | None = None
        self.last_preference_scope_request: PreferenceScopeRequest | None = None
        self.last_discovered_preference_context: PreferenceReadContext | None = None
        self._preference_discovery_active = False
        self._preference_nominee_ids: list[uuid.UUID] = []
        self._preference_cold_start = False
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
        self, query: str, external_user_id: str | None = None, proxy_user_id: str | None = None,
        user_id: str | None = None, limit: int = 10, categories: list[str] | None = None,
        agent_id: str | None = None, tenant_id: str | None = None,
        time_filter_days: int | None = None, as_of: datetime | None = None,
        quota_mode: str | QuotaMode | None = None, preference_context: PreferenceReadContext | None = None,
    ) -> list[MemoryResult]:
        self.last_memory_visibility = None
        self.last_preference_scope_request = None
        self.last_discovered_preference_context = None
        self._preference_nominee_ids = []
        self._preference_cold_start = False
        self._preference_discovery_active = (discovery_enabled(tenant_id) and preference_context is None
            and as_of is None and agent_id is None and (not categories or "preference" in categories))
        self._resolved_read_proxy_id: str | None = None
        args: dict[str, Any] = {"query": query, "external_user_id": external_user_id, "proxy_user_id": proxy_user_id,
                "user_id": user_id, "limit": limit, "categories": categories, "agent_id": agent_id,
                "tenant_id": tenant_id, "time_filter_days": time_filter_days, "as_of": as_of,
                "quota_mode": quota_mode, "preference_context": preference_context}
        with self.session.no_autoflush:
            candidates = await self._retrieve_candidates(**args)
        if self.last_memory_visibility is not None and self.last_memory_visibility.scope == "user_learned_context":
            return []  # Preflight withheld context without a provider wait.
        owner = self._resolved_read_proxy_id
        if as_of is not None or tenant_id is None or owner is None:
            return candidates
        if self.last_quota_mode in {QuotaMode.blocked.value, QuotaMode.passthrough.value}:
            return candidates
        return await self._final_current_read(candidates, owner=owner, **args)

    async def _final_current_read(self, candidates, *, owner, **args):
        """No providers: pending state, SQL hydration and winners share a snapshot.

        Release the request's read-only transaction first; never commit caller
        mutations. A separate short session cannot expire router-owned objects.
        Cache/vector delivery and job completion are not applied-receipt proofs.
        """
        await self._finish_read_transaction()
        bind = self.session.bind
        if bind is None:
            raise ValueError("governed retrieval requires a bound session")
        sessions = async_sessionmaker(bind.execution_options(isolation_level="REPEATABLE READ"),
                                      expire_on_commit=False, class_=type(self.session))
        original = self.session
        async with sessions.begin() as snapshot:
            self.session = snapshot
            try:
                row = await self._runtime_visibility_row(owner, args["tenant_id"])
                if row is None:
                    self.last_cache_hit = False
                    self.last_preference_decisions = []
                    return []
                if row[0]:
                    hold = await read_runtime_hold(self.session, owner=self._as_uuid(owner),
                        tenant_id=self._as_uuid(args["tenant_id"]), predicate=self._runtime_job_predicate(),
                        unavailable=bool(row[1]))
                    self.last_memory_visibility = MemoryVisibility(state="unavailable" if hold.unavailable else "processing",
                        scope="user_learned_context" if hold.broad else "affected_context")
                    self.last_is_degraded = True
                    self.last_cache_hit = False
                    self.last_preference_decisions = []
                    if hold.broad:
                        return []
                else:
                    hold = RuntimeHold(broad=False)
                    self.last_memory_visibility = None
                discovery = None
                if self._preference_discovery_active and discovery_enabled(args["tenant_id"]):
                    try:
                        discovery = await discover_preferences(self.session,
                            tenant_id=self._as_uuid(args["tenant_id"]), owner=self._as_uuid(owner),
                            nominee_ids=self._preference_nominee_ids or self._bounded_memory_ids(m.id for m in candidates),
                            cold_start=self._preference_cold_start, is_current=self._memory_is_current,
                            created_after=self._created_after(args["time_filter_days"]),
                            withheld_fingerprints=hold.preference_fingerprints)
                    except PreferenceAdmissionUnavailable as exc:
                        logger.warning("Preference discovery unavailable. tenant_id=%s proxy_user_id=%s reason=%s",
                                       args["tenant_id"], owner, exc)
                        self.last_memory_visibility = MemoryVisibility(state="unavailable")
                        self.last_is_degraded, self.last_cache_hit = True, False
                        self.last_preference_decisions = []
                        return []  # Incomplete coverage/configuration is not a global fallback.
                    self.last_discovered_preference_context = discovery.context
                    self.last_preference_scope_request = discovery.scope_request
                if not row[2] and discovery is None:
                    return candidates  # No server-selected runtime job for this owner.
                categories = [c.lower() for c in args["categories"] or []]
                fresh = await self._hydrate_current_results(
                    [m for m in candidates if (args["preference_context"] is None
                        and discovery is None) or m.category != "preference"],
                    proxy_user_id=owner, user_id=None, tenant_id=args["tenant_id"],
                    categories=categories, agent_id=args["agent_id"],
                    created_after=self._created_after(args["time_filter_days"]),
                )
                fresh = [m for m in fresh if m.id not in hold.fact_ids and m.previous_version_id not in hold.fact_ids
                         and not (hold.preference_fingerprints and m.category == "preference")]
                context = args["preference_context"] or (discovery.context if discovery is not None else None)
                if context is not None and (not categories or "preference" in categories):
                    preferences = await self._retrieve_declared_preferences(
                        context=context, query=args["query"],
                        identity=args["external_user_id"] or args["user_id"], proxy_user_id=owner,
                        tenant_id=args["tenant_id"], limit=args["limit"], categories=["preference"],
                        agent_id=args["agent_id"], time_filter_days=args["time_filter_days"],
                        quota_mode=self.last_quota_mode, include_other=False,
                        withheld_fingerprints=hold.preference_fingerprints,
                        rows=discovery.rows if discovery is not None else None,
                    )
                    fresh = preferences + fresh
                if row[0]:
                    # While any update is held, do not turn old choice cards into
                    # a new clarification request through a partial response.
                    self.last_preference_decisions = []
                    self.last_preference_scope_request = None
                    self.last_is_degraded = True
                self.last_cache_hit = False
                return fresh[:args["limit"]]
            finally:
                self.session = original

    async def _runtime_visibility_row(self, owner, tenant_id):
        # Partial owner indexes; no raw evidence or unbounded job loading.
        routed = select(ExtractionJob.id).where(
            ExtractionJob.tenant_id == self._as_uuid(tenant_id),
            ExtractionJob.proxy_user_id == self._as_uuid(owner), self._runtime_job_predicate(),
        )
        pending = routed.where(unresolved_runtime_predicate())
        if target_release_enabled(tenant_id):
            pending = pending.where(ExtractionJob.runtime_hold_released_at.is_(None))
        unavailable = pending.where(or_(
            ExtractionJob.status.in_([ExtractionJobStatus.failed, ExtractionJobStatus.dead]),
            ExtractionJob.payload_redacted_at.is_not(None),
            ExtractionJob.runtime_applied_at.is_not(None),
        ))
        return (await self.session.execute(select(pending.exists(), unavailable.exists(), routed.exists()).where(
            ProxyUser.id == self._as_uuid(owner), ProxyUser.tenant_id == self._as_uuid(tenant_id),
            ProxyUser.is_blocked.is_(False),
        ))).first()

    async def _finish_read_transaction(self):
        if self.session.new or self.session.dirty or self.session.deleted:
            raise ValueError("governed retrieval requires a read-only session")
        expire = self.session.sync_session.expire_on_commit
        self.session.sync_session.expire_on_commit = False
        try:
            await self.session.commit()
        finally:
            self.session.sync_session.expire_on_commit = expire

    async def _retrieve_candidates(
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
        preference_context: PreferenceReadContext | None = None,
    ) -> list[MemoryResult]:
        self.last_preference_decisions = []
        identity = external_user_id or user_id
        if not identity:
            raise ValueError("retrieve requires external_user_id or user_id.")
        normalized_categories = [category.lower() for category in categories or []]
        created_after = self._created_after(time_filter_days)
        cache_context = self._cache_context(query, normalized_categories, agent_id, limit, time_filter_days, as_of)
        if self._preference_discovery_active:
            cache_context += "|preference-discovery-v1"

        self.last_is_degraded = False

        proxy_user: ProxyUser | SimpleNamespace | None = None
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
        self._resolved_read_proxy_id = str(proxy_user.id) if proxy_user is not None else None

        resolved_quota_mode = self._coerce_quota_mode(quota_mode) if quota_mode is not None else await self._resolve_quota_mode(tenant_id)
        self.last_quota_mode = resolved_quota_mode.value
        if as_of is None and tenant_id is not None and proxy_user is not None:
            row = await self._runtime_visibility_row(str(proxy_user.id), tenant_id)
            if row is None:
                self.last_cache_hit = False
                return []
            if row[0]:
                hold = await read_runtime_hold(self.session, owner=self._as_uuid(proxy_user.id),
                    tenant_id=self._as_uuid(tenant_id), predicate=self._runtime_job_predicate(),
                    unavailable=bool(row[1]))
                if hold.broad:
                    self.last_cache_hit = False
                    self.last_memory_visibility = MemoryVisibility(state="unavailable" if hold.unavailable else "processing")
                    self.last_is_degraded = True
                    return []

        if resolved_quota_mode in {QuotaMode.blocked, QuotaMode.passthrough}:
            return []

        if preference_context is not None:
            from api.settings import get_settings
            if not get_settings().explicit_preference_claims_enabled or not tenant_id or proxy_user is None or as_of is not None:
                raise ValueError("explicit preference context unavailable")
            return await self._retrieve_declared_preferences(
                context=PreferenceReadContext.model_validate(preference_context),
                query=query, identity=identity, proxy_user_id=str(proxy_user.id), tenant_id=tenant_id,
                limit=limit, categories=normalized_categories, agent_id=agent_id,
                time_filter_days=time_filter_days, quota_mode=resolved_quota_mode,
            )

        cached_results = await self._get_cached_results(user_id=cache_identity, cache_context=cache_context) if as_of is None else None
        if cached_results:
            verified = await self._hydrate_current_results(
                cached_results, proxy_user_id=str(proxy_user.id) if proxy_user is not None else None,
                user_id=identity if proxy_user is None else None, tenant_id=tenant_id,
                categories=normalized_categories, agent_id=agent_id, created_after=created_after,
            )
            # Missing/changed candidates invalidate the whole cached ranking.
            # Recompute to discover the replacement rather than returning a
            # partial cached answer or silently retaining an obsolete score.
            cached_results = verified if (
                len(verified) == len(cached_results)
                and {row.id: row.content for row in verified}
                == {row.id: row.content for row in cached_results}
            ) else None
        else:
            cached_results = None
        if cached_results is not None:
            self.last_cache_hit = True
            self._queue_access_update([result.id for result in cached_results])
            return cached_results[:limit]
        self.last_cache_hit = False

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
        hot_tier_results = await self._hydrate_current_results(
            hot_tier_results, proxy_user_id=str(proxy_user.id) if proxy_user is not None else None,
            user_id=identity if proxy_user is None else None, tenant_id=tenant_id,
            categories=normalized_categories, agent_id=agent_id, created_after=created_after,
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
            self._preference_cold_start = self._preference_discovery_active
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
        ]

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
                created_after=created_after,
                tenant_id=tenant_id,
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
            if not self._memory_is_current(memory):
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
        if l1_cached is not None and not self._preference_discovery_active:
            expires_at, results = l1_cached
            if expires_at > now:
                return list(results)
            self._l1_cache.pop(l1_key, None)
        if not REDIS_CACHE_READ_ENABLED:
            return None

        cached_memories = await self._read_retrieval_cache(user_id, cache_context)
        if not cached_memories:
            return None

        if self._preference_discovery_active:
            if not isinstance(cached_memories[0], dict):
                return None  # Malformed cache pages are misses, not discovery evidence.
            nominees = cached_memories[0].get("_preference_nominee_ids")
            if not isinstance(nominees, list):
                return None  # Old cache pages cannot prove their preference nominations.
            self._preference_nominee_ids = self._bounded_memory_ids(nominees)
            self._preference_cold_start = cached_memories[0].get("_preference_cold_start") is True

        try:
            results = [self._memory_result_from_cache(item) for item in cached_memories[:50]]
        except (KeyError, TypeError, ValueError):
            return None
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
        if payload and self._preference_discovery_active:
            payload[0]["_preference_nominee_ids"] = [str(mid) for mid in self._preference_nominee_ids]
            payload[0]["_preference_cold_start"] = self._preference_cold_start
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

        results = []
        for item in cached_memories[:50]:
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
        query = select(Memory).where(Memory.id.in_(ids), legacy_memory_predicate())
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
                legacy_memory_predicate(),
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

        query = query.where(Memory.is_archived.is_(False), legacy_memory_predicate())
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
        result = await self.session.execute(query.execution_options(populate_existing=True))
        return [
            self._memory_to_result(memory, semantic_score=0.0)
            for memory in result.scalars().all()
            if self._memory_is_current(memory)
        ]

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
            legacy_memory_predicate(),
        )
        if categories:
            query = query.where(Memory.category.in_([category for category in categories]))
        if agent_id is not None:
            query = query.where(Memory.agent_id == self._as_uuid(agent_id))
        if created_after is not None:
            query = query.where(Memory.created_at >= created_after)

        result = await self.session.execute(query.execution_options(populate_existing=True))
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
            legacy_memory_predicate(),
        )
        if categories:
            query = query.where(Memory.category.in_([category for category in categories]))
        if agent_id is not None:
            query = query.where(Memory.agent_id == self._as_uuid(agent_id))
        if created_after is not None:
            query = query.where(Memory.created_at >= created_after)
        result = await self.session.execute(query.execution_options(populate_existing=True))
        return list(result.scalars().all())

    async def _fetch_memories_by_ids(
        self,
        *,
        memory_ids: Iterable[str | None],
        proxy_user_id: str,
        categories: list[str],
        agent_id: str | None,
        created_after: datetime | None = None,
        tenant_id: str | None = None,
    ) -> dict[str, Memory]:
        normalized_ids = self._bounded_memory_ids(memory_ids)
        if not normalized_ids:
            return {}

        if self._preference_discovery_active:
            self._preference_nominee_ids = list(dict.fromkeys((*self._preference_nominee_ids, *normalized_ids)))[:50]

        query = select(Memory).where(
            Memory.id.in_(normalized_ids),
            Memory.proxy_user_id == self._as_uuid(proxy_user_id),
            Memory.is_archived.is_(False),
            legacy_memory_predicate(),
        )
        if tenant_id is not None:
            query = query.join(ProxyUser, Memory.proxy_user_id == ProxyUser.id).where(
                ProxyUser.tenant_id == self._as_uuid(tenant_id),
                ProxyUser.is_blocked.is_(False),
            )
        if categories:
            query = query.where(Memory.category.in_([category for category in categories]))
        if agent_id is not None:
            query = query.where(Memory.agent_id == self._as_uuid(agent_id))
        if created_after is not None:
            query = query.where(Memory.created_at >= created_after)

        result = await self.session.execute(query.execution_options(populate_existing=True))
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
        normalized_ids = self._bounded_memory_ids(memory_ids)
        if not normalized_ids:
            return {}
        query = select(Memory).where(
            Memory.id.in_(normalized_ids),
            Memory.user_id == self._as_uuid(user_id),
            Memory.is_archived.is_(False),
            legacy_memory_predicate(),
        )
        if categories:
            query = query.where(Memory.category.in_([category for category in categories]))
        if agent_id is not None:
            query = query.where(Memory.agent_id == self._as_uuid(agent_id))
        if created_after is not None:
            query = query.where(Memory.created_at >= created_after)
        result = await self.session.execute(query.execution_options(populate_existing=True))
        memories = list(result.scalars().all())
        return {str(memory.id): memory for memory in memories}

    @staticmethod
    def _bounded_memory_ids(memory_ids: Iterable[str | None]) -> list[uuid.UUID]:
        ids: list[uuid.UUID] = []
        for value in islice(memory_ids, 50):
            try:
                parsed = uuid.UUID(str(value))
            except (ValueError, TypeError, AttributeError):
                continue
            if parsed not in ids:
                ids.append(parsed)
        return ids

    async def _hydrate_current_results(
        self, candidates: list[MemoryResult], *, proxy_user_id: str | None,
        user_id: str | None, tenant_id: str | None, categories: list[str],
        agent_id: str | None, created_after: datetime | None,
    ) -> list[MemoryResult]:
        """Caches nominate at most one page; SQL owns content and eligibility.

        One indexed batch read, no provider call and no locks. Always refresh
        ORM instances: another process may have committed since the last read.
        A database failure propagates; stale context is never a fallback.
        """
        if not candidates:
            return []
        memory_ids = [row.id for row in candidates[:50]]
        if proxy_user_id is not None:
            memories = await self._fetch_memories_by_ids(
                memory_ids=memory_ids, categories=categories, agent_id=agent_id,
                created_after=created_after, proxy_user_id=proxy_user_id, tenant_id=tenant_id,
            )
        elif user_id is not None:
            memories = await self._fetch_memories_by_ids_for_user(
                memory_ids=memory_ids, categories=categories, agent_id=agent_id,
                created_after=created_after, user_id=user_id,
            )
        else:
            return []
        results = [
            self._memory_to_result(memories[row.id], semantic_score=row.semantic_score)
            for row in candidates[:50]
            if row.id in memories and self._memory_is_current(memories[row.id])
        ]
        return sorted(self._deduplicate_results(results), key=lambda row: row.final_score, reverse=True)

    async def _embed_query(self, query: str, *, model_id: str) -> EmbeddingResult:
        model = await self.embedding_service.get_model(model_id)
        await self._finish_read_transaction()
        return await self.embedding_service.embed(query, model_record=model)

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

    async def _retrieve_declared_preferences(
        self, *, context, query, identity, proxy_user_id, tenant_id, limit,
        categories, agent_id, time_filter_days, quota_mode, include_other=True, withheld_fingerprints=(), rows=None,
    ) -> list[MemoryResult]:
        # <=32 indexed identities, one SQL round-trip. A vector page is not a
        # complete property set, so it cannot decide which scope wins.
        fingerprints = {
            preference_fingerprint(key, scope)
            for key in context.property_keys
            for scope in (PreferenceApplicability(kind="global"), context.applicability)
        }
        preferences = []
        decisions = []
        if not categories or "preference" in categories:
            if rows is None:
                rows = await preference_rows(self.session, tenant_id=self._as_uuid(tenant_id),
                    owner=self._as_uuid(proxy_user_id), fingerprints=fingerprints)
            by_fingerprint = {claim.claim_fingerprint: (claim, memory) for claim, memory in rows}
            created_after = self._created_after(time_filter_days)
            for key in context.property_keys:
                if any(preference_fingerprint(key, scope) in withheld_fingerprints
                       for scope in (context.applicability, PreferenceApplicability(kind="global"))):
                    continue  # A held scoped identity cannot reactivate a global fallback.
                selected = None
                for scope in (context.applicability, PreferenceApplicability(kind="global")):
                    pair = by_fingerprint.get(preference_fingerprint(key, scope))
                    if pair is None or pair[0].active_memory_id is None:
                        continue
                    claim, memory = pair
                    if memory is not None and held_choice(memory, claim):
                        decisions.append(PreferenceReadDecision(property_key=key, applicability=scope))
                        break  # Never use a broader default for this unresolved choice.
                    # An inconsistent scoped winner blocks fallback, rather than
                    # silently declaring a broader value current.
                    if memory is None or str(memory.proxy_user_id) != proxy_user_id or claim.status not in {"active", "archived"}:
                        break
                    declaration = preference_declaration((memory.metadata_json or {}).get("provenance"))
                    if declaration is None or declaration.property_key != key or declaration.applicability != scope or declaration.value != claim.active_value:
                        break
                    if claim.status == "archived" and not memory.is_archived:
                        break
                    if memory.is_archived or not self._memory_is_current(memory):
                        continue
                    if agent_id is not None and str(memory.agent_id) != str(agent_id):
                        continue
                    if created_after is not None and memory.created_at < created_after:
                        continue
                    selected = memory
                    break
                if selected is not None:
                    preferences.append(self._memory_to_result(selected, semantic_score=1.0))
        # Other categories retain their existing ranking and cache behavior;
        # unknown legacy preferences are not promoted to global in this mode.
        other_categories = [c for c in (categories or ["fact", "goal", "procedure", "relationship", "expertise"]) if c != "preference"]
        other = await self._retrieve_candidates(
            query=query, external_user_id=identity, proxy_user_id=proxy_user_id,
            tenant_id=tenant_id, categories=other_categories, agent_id=agent_id,
            limit=limit, time_filter_days=time_filter_days, quota_mode=quota_mode,
        ) if include_other and other_categories else []
        self.last_cache_hit = False  # Preference winners are always freshly read.
        self.last_preference_decisions = decisions
        self._queue_access_update([result.id for result in preferences])
        return (preferences + other)[:limit]

    def _memory_to_result(self, memory: Memory, *, semantic_score: float) -> MemoryResult:
        from api.services.preference_claim_service import preference_provenance
        provenance = preference_provenance(memory)
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
            provenance=provenance,
            effective_from=memory.effective_from.isoformat() if memory.effective_from else None,
            effective_until=memory.effective_until.isoformat() if memory.effective_until else None,
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
        return cls._is_valid_at(None, getattr(memory, "expires_at", None), reference) and cls._is_valid_at(
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
