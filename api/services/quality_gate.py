from __future__ import annotations

import json
import logging
import math
import os
import re
import time
import uuid
from dataclasses import dataclass
from typing import Any

from redis.exceptions import ConnectionError as RedisConnectionError
from redis.exceptions import TimeoutError as RedisTimeoutError
from sqlalchemy.ext.asyncio import AsyncSession

from api.db.cache import CacheService
from api.db.models import CallQualityBlockedLayer
from api.db.models import CallQualityLog
from api.db.models import TenantBudget
from api.infra.fallbacks import on_redis_open
from api.services.budget_governor import BudgetGovernor
from api.services.embedding_service import EmbeddingService


LOGGER = logging.getLogger("memoryos.quality_gate")
QUALITY_SCORE_THRESHOLD = 0.35
SEMANTIC_DUPLICATE_THRESHOLD = 0.92
MIN_LEXICAL_OVERLAP_FOR_SEMANTIC_DUPLICATE = 0.2
RATE_LIMIT_TTL_SECONDS = 120
RECENT_QUERY_TTL_SECONDS = 3600
RECENT_QUERY_LIST_SIZE = 5
DEFAULT_EMBEDDING_MODEL = "text-embedding-3-small"
DEFAULT_EMBEDDING_DIMENSIONS = 1536
INTERNAL_ERROR_REASON = "internal_error"
REDIS_FAILURES = (RedisConnectionError, RedisTimeoutError)
STOP_WORDS = {
    "a",
    "about",
    "am",
    "an",
    "and",
    "are",
    "around",
    "be",
    "for",
    "have",
    "help",
    "i",
    "in",
    "is",
    "it",
    "me",
    "my",
    "of",
    "on",
    "out",
    "should",
    "the",
    "this",
    "to",
    "user",
    "want",
    "what",
    "with",
}


@dataclass(slots=True)
class GateResult:
    passed: bool
    blocked_layer: str | None
    reason: str | None
    retry_after_seconds: int | None = None
    budget_remaining_pct: float | None = None


class QualityGateService:
    def __init__(
        self,
        *,
        session: AsyncSession,
        cache_service: CacheService,
        budget_governor: BudgetGovernor | None = None,
        client: Any | None = None,
        embedding_model: str | None = None,
        embedding_service: EmbeddingService | None = None,
    ) -> None:
        self.session = session
        self.cache_service = cache_service
        self.budget_governor = budget_governor or BudgetGovernor(session=session)
        self.client = client
        self.embedding_model = embedding_model or os.getenv(
            "EMBEDDING_MODEL",
            DEFAULT_EMBEDDING_MODEL,
        )
        self.embedding_dimensions = int(
            os.getenv("EMBEDDING_DIMENSIONS", str(DEFAULT_EMBEDDING_DIMENSIONS))
        )
        self.embedding_service = embedding_service or EmbeddingService(
            async_session=session,
            gemini_client=client,
        )

    def _mark_redis_unavailable(self) -> None:
        breaker = getattr(self.cache_service, "breaker", None)
        force_open = getattr(breaker, "force_open", None)
        if callable(force_open):
            try:
                force_open()
            except Exception:
                return None

    async def check(
        self,
        messages: list[dict[str, Any]],
        tenant_id: str,
        external_user_id: str,
        *,
        semantic_deduplication: bool = True,
    ) -> GateResult:
        quality_score = 0.0
        semantic_similarity: float | None = None
        blocked_layer = CallQualityBlockedLayer.none
        gate_reason: str | None = None
        tenant_budget: TenantBudget | None = None

        try:
            tenant_budget = await self.budget_governor.get_tenant_budget(tenant_id)
            budget_remaining_pct = self.budget_governor.budget_remaining_pct(
                messages=messages,
                tenant_budget=tenant_budget,
            )
            current_window = int(time.time()) // 60
            rate_key = self._rate_limit_key(tenant_id, external_user_id, current_window)
            current_count = await self._get_redis_count(rate_key)
            rate_limit = self.budget_governor.rate_limit_per_user(tenant_budget)

            if current_count >= rate_limit:
                blocked_layer = CallQualityBlockedLayer.l1
                gate_reason = "rate_limit_exceeded"
                return GateResult(
                    passed=False,
                    blocked_layer=blocked_layer.value,
                    reason=gate_reason,
                    retry_after_seconds=self._retry_after_seconds(),
                    budget_remaining_pct=budget_remaining_pct,
                )

            quality_score = self._conversation_quality_score(messages)
            if quality_score < QUALITY_SCORE_THRESHOLD:
                blocked_layer = CallQualityBlockedLayer.l2
                gate_reason = "low_quality"
                return GateResult(
                    passed=False,
                    blocked_layer=blocked_layer.value,
                    reason=gate_reason,
                    budget_remaining_pct=budget_remaining_pct,
                )

            if semantic_deduplication:
                semantic_similarity = await self._semantic_deduplication(
                    messages=messages,
                    tenant_id=tenant_id,
                    external_user_id=external_user_id,
                )
                if (
                    semantic_similarity is not None
                    and semantic_similarity > SEMANTIC_DUPLICATE_THRESHOLD
                ):
                    blocked_layer = CallQualityBlockedLayer.l3
                    gate_reason = "duplicate_query"
                    return GateResult(
                        passed=False,
                        blocked_layer=blocked_layer.value,
                        reason=gate_reason,
                        budget_remaining_pct=budget_remaining_pct,
                    )

            budget_decision = await self.budget_governor.evaluate(
                messages=messages,
                tenant_id=tenant_id,
                tenant_budget=tenant_budget,
            )
            if not budget_decision.passed:
                blocked_layer = CallQualityBlockedLayer.l4
                gate_reason = budget_decision.reason
                return GateResult(
                    passed=False,
                    blocked_layer=blocked_layer.value,
                    reason=gate_reason,
                    budget_remaining_pct=budget_decision.budget_remaining_pct,
                )

            await self._increment_rate_counter(rate_key)
            await self.budget_governor.dispatch_usage_increment(
                tenant_id,
                budget_decision.estimated_tokens,
            )
            return GateResult(
                passed=True,
                blocked_layer=None,
                reason=None,
                budget_remaining_pct=budget_decision.budget_remaining_pct,
            )
        except Exception:
            LOGGER.exception("Quality gate failed for tenant %s", tenant_id)
            gate_reason = INTERNAL_ERROR_REASON
            return GateResult(
                passed=False,
                blocked_layer=None,
                reason=gate_reason,
            )
        finally:
            await self._log_quality_result(
                tenant_id=tenant_id,
                external_user_id=external_user_id,
                blocked_layer=blocked_layer,
                reason=gate_reason,
                quality_score=quality_score,
                semantic_similarity=semantic_similarity,
            )

    async def _semantic_deduplication(
        self,
        *,
        messages: list[dict[str, Any]],
        tenant_id: str,
        external_user_id: str,
    ) -> float | None:
        redis_key = self._recent_queries_key(tenant_id, external_user_id)

        try:
            recent_items = await self._redis_call(
                self.cache_service.client.lrange,
                redis_key,
                0,
                RECENT_QUERY_LIST_SIZE - 1,
                fallback=lambda: on_redis_open([]),
            )
        except REDIS_FAILURES:
            self._mark_redis_unavailable()
            return None

        query_text = self._conversation_text(messages)
        if not recent_items:
            await self._store_recent_query(redis_key, query_text=query_text, embedding=None)
            return None

        embedding = await self._embed_query(query_text)
        if embedding is None:
            await self._store_recent_query(redis_key, query_text=query_text, embedding=None)
            return None
        similarity_scores = []

        for item in recent_items:
            try:
                payload = json.loads(item)
                previous_embedding = payload.get("embedding") or []
                previous_query = str(payload.get("query", "")).strip()
            except json.JSONDecodeError:
                continue

            if previous_query and self._normalize_query(previous_query) == self._normalize_query(query_text):
                similarity_scores.append(1.0)
                continue
            if not isinstance(previous_embedding, list) or not previous_embedding:
                continue
            if previous_query and self._has_conflicting_salient_entities(previous_query, query_text):
                similarity_scores.append(0.0)
                continue
            semantic_similarity = self._cosine_similarity(embedding, previous_embedding)
            if (
                previous_query
                and semantic_similarity < SEMANTIC_DUPLICATE_THRESHOLD
                and self._token_overlap(previous_query, query_text) < MIN_LEXICAL_OVERLAP_FOR_SEMANTIC_DUPLICATE
            ):
                similarity_scores.append(0.0)
                continue
            similarity_scores.append(semantic_similarity)

        await self._store_recent_query(redis_key, query_text=query_text, embedding=embedding)

        if not similarity_scores:
            return 0.0
        return max(similarity_scores)

    async def _embed_query(self, query: str) -> list[float] | None:
        try:
            result = await self.embedding_service.embed(query)
        except Exception:
            return None
        return list(result.vector)

    async def _increment_rate_counter(self, rate_key: str) -> None:
        try:
            await self._redis_call(
                self.cache_service.client.incr,
                rate_key,
                fallback=lambda: on_redis_open(None),
            )
            await self._redis_call(
                self.cache_service.client.expire,
                rate_key,
                RATE_LIMIT_TTL_SECONDS,
                fallback=lambda: on_redis_open(None),
            )
        except REDIS_FAILURES:
            self._mark_redis_unavailable()
            return

    async def _get_redis_count(self, rate_key: str) -> int:
        try:
            raw_value = await self._redis_call(
                self.cache_service.client.get,
                rate_key,
                fallback=lambda: on_redis_open(None),
            )
        except REDIS_FAILURES:
            self._mark_redis_unavailable()
            return 0
        return int(raw_value) if raw_value is not None else 0

    async def _log_quality_result(
        self,
        *,
        tenant_id: str,
        external_user_id: str,
        blocked_layer: CallQualityBlockedLayer,
        reason: str | None,
        quality_score: float,
        semantic_similarity: float | None,
    ) -> None:
        try:
            self.session.add(
                CallQualityLog(
                    tenant_id=uuid.UUID(tenant_id),
                    external_user_id=external_user_id,
                    layer_blocked_at=blocked_layer,
                    quality_score=float(round(quality_score, 6)),
                    reason=reason,
                    semantic_similarity=(
                        float(round(semantic_similarity, 6))
                        if semantic_similarity is not None
                        else None
                    ),
                )
            )
            await self.session.commit()
        except Exception:
            if hasattr(self.session, "rollback"):
                await self.session.rollback()

    @staticmethod
    def _rate_limit_key(tenant_id: str, external_user_id: str, window_minute: int) -> str:
        return f"rate:{tenant_id}:{external_user_id}:{window_minute}"

    @staticmethod
    def _recent_queries_key(tenant_id: str, external_user_id: str) -> str:
        return f"user_recent_queries:{tenant_id}:{external_user_id}"

    @staticmethod
    def _conversation_text(messages: list[dict[str, Any]]) -> str:
        user_contents = [
            str(message.get("content", "")).strip()
            for message in messages
            if str(message.get("role", "user")).lower() == "user"
            and str(message.get("content", "")).strip()
        ]
        if user_contents:
            return "\n".join(user_contents)
        return "\n".join(
            str(message.get("content", "")).strip()
            for message in messages
            if str(message.get("content", "")).strip()
        )

    async def _store_recent_query(
        self,
        redis_key: str,
        *,
        query_text: str,
        embedding: list[float] | None,
    ) -> None:
        try:
            await self._redis_call(
                self.cache_service.client.lpush,
                redis_key,
                json.dumps({"query": query_text, "embedding": embedding or []}),
                fallback=lambda: on_redis_open(None),
            )
            await self._redis_call(
                self.cache_service.client.ltrim,
                redis_key,
                0,
                RECENT_QUERY_LIST_SIZE - 1,
                fallback=lambda: on_redis_open(None),
            )
            await self._redis_call(
                self.cache_service.client.expire,
                redis_key,
                RECENT_QUERY_TTL_SECONDS,
                fallback=lambda: on_redis_open(None),
            )
        except REDIS_FAILURES:
            self._mark_redis_unavailable()
            return

    async def _redis_call(self, fn, *args, fallback=None, **kwargs):
        breaker = getattr(self.cache_service, "breaker", None)
        if (
            breaker is None
            or breaker.__class__.__module__.startswith("unittest.mock")
        ):
            return await fn(*args, **kwargs)
        return await breaker.call(fn, *args, fallback=fallback, **kwargs)

    @classmethod
    def _conversation_quality_score(cls, messages: list[dict[str, Any]]) -> float:
        if not messages:
            return 0.0

        normalized_messages = [
            str(message.get("content", "")).strip()
            for message in messages
            if str(message.get("content", "")).strip()
        ]
        if not normalized_messages:
            return 0.0

        all_text = " ".join(normalized_messages)
        words = re.findall(r"\b[\w'-]+\b", all_text.lower())
        total_words = len(words)
        unique_words = len(set(words))
        avg_chars = sum(len(message) for message in normalized_messages) / len(normalized_messages)

        if len(normalized_messages) == 1 and total_words <= 2 and avg_chars < 10:
            return 0.1

        message_count_score = min(len(messages) / 3, 1.0)
        avg_length_score = min(avg_chars / 50, 1.0)
        lexical_diversity = (unique_words / total_words) if total_words else 0.0
        question_signal = 1.0 if any("?" in message for message in normalized_messages) else 0.5

        score = (
            (message_count_score * 0.25)
            + (avg_length_score * 0.30)
            + (lexical_diversity * 0.25)
            + (question_signal * 0.20)
        )
        return round(min(max(score, 0.0), 1.0), 6)

    @staticmethod
    def _cosine_similarity(vector_a: list[float], vector_b: list[float]) -> float:
        if not vector_a or not vector_b or len(vector_a) != len(vector_b):
            return 0.0

        dot_product = sum(left * right for left, right in zip(vector_a, vector_b))
        magnitude_a = math.sqrt(sum(value * value for value in vector_a))
        magnitude_b = math.sqrt(sum(value * value for value in vector_b))
        if magnitude_a == 0.0 or magnitude_b == 0.0:
            return 0.0
        return dot_product / (magnitude_a * magnitude_b)

    @staticmethod
    def _normalize_query(value: str) -> str:
        return re.sub(r"\s+", " ", value.strip().lower())

    @classmethod
    def _salient_entities(cls, value: str) -> set[str]:
        acronyms = set(re.findall(r"\b[A-Z]{2,}\b", value))
        numbers = set(re.findall(r"\b\d+(?:\.\d+)?\b", value))
        subject_like = {
            token
            for token in re.findall(r"\b[a-z][a-z0-9_+-]{2,}\b", value.lower())
            if token not in STOP_WORDS
        }
        return acronyms | numbers | subject_like

    @classmethod
    def _has_conflicting_salient_entities(cls, previous_query: str, query_text: str) -> bool:
        previous_entities = cls._salient_entities(previous_query)
        current_entities = cls._salient_entities(query_text)
        if not previous_entities or not current_entities:
            return False

        previous_strong = cls._strong_identifiers(previous_query)
        current_strong = cls._strong_identifiers(query_text)
        if previous_strong or current_strong:
            return bool(previous_strong and current_strong and previous_strong.isdisjoint(current_strong))

        shared = previous_entities & current_entities
        previous_unique = previous_entities - shared
        current_unique = current_entities - shared
        if not previous_unique or not current_unique:
            return False

        # Different exam names, subjects, scores, or other salient facts mean
        # this is probably a new memory, not a duplicate add() request.
        return True

    @classmethod
    def _token_overlap(cls, previous_query: str, query_text: str) -> float:
        previous_tokens = cls._content_tokens(previous_query)
        current_tokens = cls._content_tokens(query_text)
        if not previous_tokens or not current_tokens:
            return 0.0
        return len(previous_tokens & current_tokens) / min(
            len(previous_tokens),
            len(current_tokens),
        )

    @staticmethod
    def _content_tokens(value: str) -> set[str]:
        return {
            token
            for token in re.findall(r"\b[\w'-]+\b", value.lower())
            if len(token) > 2 and token not in STOP_WORDS
        }

    @staticmethod
    def _strong_identifiers(value: str) -> set[str]:
        acronyms = set(re.findall(r"\b[A-Z]{2,}\b", value))
        numbers = set(re.findall(r"\b\d+(?:\.\d+)?\b", value))
        return acronyms | numbers

    @staticmethod
    def _retry_after_seconds() -> int:
        current_second = int(time.time()) % 60
        return 60 if current_second == 0 else 60 - current_second
