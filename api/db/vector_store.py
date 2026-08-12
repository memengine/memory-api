from __future__ import annotations

import asyncio
from datetime import datetime
import os
import time
from typing import Any

from qdrant_client import AsyncQdrantClient
from qdrant_client import QdrantClient
from qdrant_client.http.exceptions import ResponseHandlingException
from qdrant_client.http import models as qmodels

from api.infra.circuit_breaker import CircuitOpenError
from api.infra.circuit_breaker_registry import CircuitBreakerRegistry
from api.infra.fallbacks import on_qdrant_open
from api.settings import get_settings


def get_qdrant_url(url: str | None = None) -> str:
    resolved_url = url or os.getenv("QDRANT_URL") or get_settings().qdrant_url
    if not resolved_url:
        raise RuntimeError("QDRANT_URL is required.")
    return resolved_url


class QdrantService:
    COLLECTION_NAME = "memories"
    UNIVERSAL_COLLECTION_NAME = "universal_memories"
    VECTOR_SIZE = 1536
    MAX_RETRIES = 3
    INITIAL_BACKOFF_SECONDS = 0.5
    DEFAULT_TIMEOUT_SECONDS = 1.0

    _shared_client: QdrantClient | None = None
    _shared_async_client: AsyncQdrantClient | None = None
    _initialized_collections: set[str] = set()

    def __init__(
        self,
        url: str | None = None,
        api_key: str | None = None,
        client: QdrantClient | None = None,
    ) -> None:
        self.breaker = CircuitBreakerRegistry.get_instance().qdrant_cb
        self.async_client: AsyncQdrantClient | None = None
        if client is not None:
            self.client = client
            self._ensure_collection_if_possible(self.COLLECTION_NAME, self.VECTOR_SIZE)
            self._ensure_collection_if_possible(self.UNIVERSAL_COLLECTION_NAME, self.VECTOR_SIZE)
            return

        if self.__class__._shared_client is None:
            resolved_api_key = (
                (api_key or None)
                if api_key is not None
                else os.getenv("QDRANT_API_KEY") or get_settings().qdrant_api_key
            )
            self.__class__._shared_client = QdrantClient(
                url=get_qdrant_url(url),
                api_key=resolved_api_key,
                timeout=float(os.getenv("QDRANT_TIMEOUT_SECONDS", self.DEFAULT_TIMEOUT_SECONDS)),
            )
        self.client = self.__class__._shared_client
        if self.__class__._shared_async_client is None:
            resolved_async_api_key = (
                (api_key or None)
                if api_key is not None
                else os.getenv("QDRANT_API_KEY") or get_settings().qdrant_api_key
            )
            self.__class__._shared_async_client = AsyncQdrantClient(
                url=get_qdrant_url(url),
                api_key=resolved_async_api_key,
                timeout=int(float(os.getenv("QDRANT_TIMEOUT_SECONDS", "10"))),
                prefer_grpc=os.getenv("QDRANT_PREFER_GRPC", "false").lower() in {"1", "true", "yes"},
                pool_size=int(os.getenv("QDRANT_ASYNC_POOL_SIZE", "100")),
            )
        self.async_client = self.__class__._shared_async_client
        self._ensure_collection_if_possible(self.COLLECTION_NAME, self.VECTOR_SIZE)
        self._ensure_collection_if_possible(self.UNIVERSAL_COLLECTION_NAME, self.VECTOR_SIZE)

    @classmethod
    def _reset_shared_state(cls) -> None:
        cls._shared_client = None
        cls._shared_async_client = None
        cls._initialized_collections = set()

    def _ensure_collection_if_possible(
        self,
        collection_name: str | None = None,
        vector_size: int | None = None,
    ) -> None:
        target_collection = collection_name or self.COLLECTION_NAME
        target_size = int(vector_size or self.VECTOR_SIZE)
        if target_collection in self.__class__._initialized_collections:
            return
        try:
            self._ensure_collection(target_collection, target_size)
            self.__class__._initialized_collections.add(target_collection)
        except Exception:
            return

    def _with_retries(self, operation: Any, *args: Any, **kwargs: Any) -> Any:
        fallback = kwargs.pop("_fallback", None)
        fallback_on_error = bool(kwargs.pop("_fallback_on_error", False))
        max_retries = int(kwargs.pop("_max_retries", self.MAX_RETRIES))
        initial_backoff = float(kwargs.pop("_initial_backoff_seconds", self.INITIAL_BACKOFF_SECONDS))
        last_error: ConnectionError | None = None

        for attempt in range(max_retries):
            try:
                return self.breaker.call_sync(operation, *args, fallback=fallback, **kwargs)
            except ResponseHandlingException:
                if fallback_on_error and fallback is not None:
                    self.breaker.force_open()
                    return fallback()
                raise
            except CircuitOpenError:
                if fallback is not None:
                    return fallback()
                raise
            except ConnectionError as error:
                last_error = error
                if attempt == max_retries - 1:
                    break
                time.sleep(initial_backoff * (2**attempt))

        if self.breaker.current_state() == "OPEN" and fallback is not None:
            return fallback()
        if last_error is not None:
            raise last_error
        raise RuntimeError("Retry wrapper exited without a result or exception.")

    def _ensure_collection(self, collection_name: str, vector_size: int) -> None:
        collection_exists = self._with_retries(
            self.client.collection_exists,
            collection_name,
        )

        if not collection_exists:
            self._with_retries(
                self.client.create_collection,
                collection_name=collection_name,
                vectors_config=qmodels.VectorParams(
                    size=vector_size,
                    distance=qmodels.Distance.COSINE,
                ),
            )

        index_fields = self._payload_indexes_for_collection(collection_name)
        for field_name, field_schema in index_fields:
            self._with_retries(
                self.client.create_payload_index,
                collection_name=collection_name,
                field_name=field_name,
                field_schema=field_schema,
                wait=True,
            )

    def _payload_indexes_for_collection(
        self,
        collection_name: str,
    ) -> tuple[tuple[str, qmodels.PayloadSchemaType], ...]:
        if collection_name == self.UNIVERSAL_COLLECTION_NAME:
            return (
                ("user_uui_id", qmodels.PayloadSchemaType.KEYWORD),
                ("source_agent_id", qmodels.PayloadSchemaType.KEYWORD),
                ("category", qmodels.PayloadSchemaType.KEYWORD),
                ("importance_score", qmodels.PayloadSchemaType.FLOAT),
                ("is_archived", qmodels.PayloadSchemaType.BOOL),
                ("created_at", qmodels.PayloadSchemaType.DATETIME),
            )

        return (
            ("tenant_id", qmodels.PayloadSchemaType.KEYWORD),
            ("proxy_user_id", qmodels.PayloadSchemaType.KEYWORD),
            ("agent_id", qmodels.PayloadSchemaType.KEYWORD),
            ("category", qmodels.PayloadSchemaType.KEYWORD),
            ("importance_score", qmodels.PayloadSchemaType.FLOAT),
            ("is_archived", qmodels.PayloadSchemaType.BOOL),
            ("created_at", qmodels.PayloadSchemaType.DATETIME),
        )

    def upsert_memory(
        self,
        memory_id: str,
        embedding: list[float],
        payload: dict[str, Any],
        *,
        collection_name: str | None = None,
        vector_size: int | None = None,
    ) -> bool:
        target_collection = collection_name or str(payload.get("qdrant_collection") or self.COLLECTION_NAME)
        target_size = int(vector_size or len(embedding) or self.VECTOR_SIZE)
        self._ensure_collection_if_possible(target_collection, target_size)
        point = qmodels.PointStruct(
            id=str(memory_id),
            vector=embedding,
            payload=payload,
        )
        self._with_retries(
            self.client.upsert,
            collection_name=target_collection,
            points=[point],
            wait=True,
        )
        return True

    def search_memories(
        self,
        query_embedding: list[float],
        tenant_id: str | None = None,
        proxy_user_id: str | None = None,
        user_id: str | None = None,
        limit: int = 20,
        category_filter: str | None = None,
        agent_id: str | None = None,
        include_archived: bool = False,
        collection_name: str | None = None,
        collection_names: list[str] | None = None,
        created_after: datetime | str | None = None,
    ) -> list[qmodels.ScoredPoint]:
        target_collections = collection_names or [collection_name or self.COLLECTION_NAME]
        for target_collection in target_collections:
            self._ensure_collection_if_possible(target_collection)
        must_conditions = self._search_conditions(
            tenant_id=tenant_id,
            proxy_user_id=proxy_user_id,
            user_id=user_id,
            category_filter=category_filter,
            agent_id=agent_id,
            include_archived=include_archived,
            created_after=created_after,
        )

        merged_points: dict[str, qmodels.ScoredPoint] = {}
        for target_collection in target_collections:
            response = self._with_retries(
                self.client.query_points,
                collection_name=target_collection,
                query=query_embedding,
                query_filter=qmodels.Filter(must=must_conditions),
                limit=limit,
                with_payload=True,
                with_vectors=False,
                _fallback=on_qdrant_open,
                _fallback_on_error=True,
            )
            points = response if isinstance(response, list) else list(response.points)
            for point in points:
                point_id = str(getattr(point, "id", ""))
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

    @staticmethod
    def _search_conditions(
        *,
        tenant_id: str | None,
        proxy_user_id: str | None,
        user_id: str | None,
        category_filter: str | None,
        agent_id: str | None,
        include_archived: bool,
        created_after: datetime | str | None,
    ) -> list[qmodels.FieldCondition]:
        must_conditions: list[qmodels.FieldCondition] = []

        if tenant_id is not None and proxy_user_id is not None:
            must_conditions.extend(
                [
                    qmodels.FieldCondition(
                        key="tenant_id",
                        match=qmodels.MatchValue(value=str(tenant_id)),
                    ),
                    qmodels.FieldCondition(
                        key="proxy_user_id",
                        match=qmodels.MatchValue(value=str(proxy_user_id)),
                    ),
                ]
            )
        elif user_id is not None:
            must_conditions.append(
                qmodels.FieldCondition(
                    key="user_id",
                    match=qmodels.MatchValue(value=str(user_id)),
                )
            )
        else:
            raise ValueError("search_memories requires tenant_id+proxy_user_id or legacy user_id.")

        if category_filter is not None:
            must_conditions.append(
                qmodels.FieldCondition(
                    key="category",
                    match=qmodels.MatchValue(value=category_filter),
                )
            )

        if agent_id is not None:
            must_conditions.append(
                qmodels.FieldCondition(
                    key="agent_id",
                    match=qmodels.MatchValue(value=str(agent_id)),
                )
            )

        if not include_archived:
            must_conditions.append(
                qmodels.FieldCondition(
                    key="is_archived",
                    match=qmodels.MatchValue(value=False),
                )
            )

        if created_after is not None:
            created_at_value = created_after if isinstance(created_after, datetime) else str(created_after)
            must_conditions.append(
                qmodels.FieldCondition(
                    key="created_at",
                    range=qmodels.DatetimeRange(gte=created_at_value),
                )
            )

        return must_conditions

    async def search_memories_async(
        self,
        query_embedding: list[float],
        tenant_id: str | None = None,
        proxy_user_id: str | None = None,
        user_id: str | None = None,
        limit: int = 20,
        category_filter: str | None = None,
        agent_id: str | None = None,
        include_archived: bool = False,
        collection_name: str | None = None,
        collection_names: list[str] | None = None,
        created_after: datetime | str | None = None,
    ) -> list[qmodels.ScoredPoint]:
        if self.async_client is None:
            return await asyncio.to_thread(
                self.search_memories,
                query_embedding=query_embedding,
                tenant_id=tenant_id,
                proxy_user_id=proxy_user_id,
                user_id=user_id,
                limit=limit,
                category_filter=category_filter,
                agent_id=agent_id,
                include_archived=include_archived,
                collection_name=collection_name,
                collection_names=collection_names,
                created_after=created_after,
            )

        target_collections = collection_names or [collection_name or self.COLLECTION_NAME]
        must_conditions = self._search_conditions(
            tenant_id=tenant_id,
            proxy_user_id=proxy_user_id,
            user_id=user_id,
            category_filter=category_filter,
            agent_id=agent_id,
            include_archived=include_archived,
            created_after=created_after,
        )
        merged_points: dict[str, qmodels.ScoredPoint] = {}
        for target_collection in target_collections:
            response = await self.breaker.call(
                self.async_client.query_points,
                collection_name=target_collection,
                query=query_embedding,
                query_filter=qmodels.Filter(must=must_conditions),
                limit=limit,
                with_payload=True,
                with_vectors=False,
                fallback=on_qdrant_open,
            )
            points = response if isinstance(response, list) else list(response.points)
            for point in points:
                point_id = str(getattr(point, "id", ""))
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

    def delete_memory(self, memory_id: str, *, collection_name: str | None = None) -> bool:
        target_collection = collection_name or self.COLLECTION_NAME
        self._ensure_collection_if_possible(target_collection)
        self._with_retries(
            self.client.delete,
            collection_name=target_collection,
            points_selector=qmodels.PointIdsList(points=[str(memory_id)]),
            wait=True,
        )
        return True

    def delete_user_memories(self, user_id: str) -> int:
        target_collection = self.COLLECTION_NAME
        self._ensure_collection_if_possible(target_collection)
        user_filter = qmodels.Filter(
            must=[
                qmodels.FieldCondition(
                    key="user_id",
                    match=qmodels.MatchValue(value=str(user_id)),
                )
            ]
        )

        count_result = self._with_retries(
            self.client.count,
            collection_name=target_collection,
            count_filter=user_filter,
            exact=True,
        )
        deleted_count = int(count_result.count)

        if deleted_count == 0:
            return 0

        self._with_retries(
            self.client.delete,
            collection_name=target_collection,
            points_selector=qmodels.FilterSelector(filter=user_filter),
            wait=True,
        )
        return deleted_count

    def delete_proxy_user_memories(
        self,
        tenant_id: str,
        proxy_user_id: str,
        *,
        collection_names: list[str] | None = None,
    ) -> int:
        target_collections = collection_names or [self.COLLECTION_NAME]
        for target_collection in target_collections:
            self._ensure_collection_if_possible(target_collection)
        proxy_filter = qmodels.Filter(
            must=[
                qmodels.FieldCondition(
                    key="tenant_id",
                    match=qmodels.MatchValue(value=str(tenant_id)),
                ),
                qmodels.FieldCondition(
                    key="proxy_user_id",
                    match=qmodels.MatchValue(value=str(proxy_user_id)),
                ),
            ]
        )

        deleted_count = 0
        for target_collection in target_collections:
            count_result = self._with_retries(
                self.client.count,
                collection_name=target_collection,
                count_filter=proxy_filter,
                exact=True,
            )
            collection_deleted = int(count_result.count)
            if collection_deleted == 0:
                continue

            self._with_retries(
                self.client.delete,
                collection_name=target_collection,
                points_selector=qmodels.FilterSelector(filter=proxy_filter),
                wait=True,
            )
            deleted_count += collection_deleted
        return deleted_count

    def delete_universal_user_memories(
        self,
        user_uui_id: str,
        *,
        collection_name: str = "universal_memories",
    ) -> int:
        target_collection = collection_name
        self._ensure_collection_if_possible(target_collection)
        user_filter = qmodels.Filter(
            must=[
                qmodels.FieldCondition(
                    key="user_uui_id",
                    match=qmodels.MatchValue(value=str(user_uui_id)),
                )
            ]
        )

        count_result = self._with_retries(
            self.client.count,
            collection_name=target_collection,
            count_filter=user_filter,
            exact=True,
        )
        deleted_count = int(count_result.count)

        if deleted_count == 0:
            return 0

        self._with_retries(
            self.client.delete,
            collection_name=target_collection,
            points_selector=qmodels.FilterSelector(filter=user_filter),
            wait=True,
        )
        return deleted_count

    def get_collection_stats(self, collection_name: str | None = None) -> dict[str, Any]:
        target_collection = collection_name or self.COLLECTION_NAME
        self._ensure_collection_if_possible(target_collection)
        stats = self._with_retries(
            self.client.get_collection,
            target_collection,
        )

        if hasattr(stats, "model_dump"):
            data = stats.model_dump()
        elif hasattr(stats, "dict"):
            data = stats.dict()
        elif isinstance(stats, dict):
            data = dict(stats)
        else:
            data = {"collection_name": self.COLLECTION_NAME, "details": stats}

        data.setdefault(
            "vectors_count",
            data.get("indexed_vectors_count", data.get("points_count", 0)),
        )
        data.setdefault("collection_name", target_collection)
        return data
