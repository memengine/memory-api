from __future__ import annotations

import logging
import uuid
from datetime import UTC
from datetime import datetime
from typing import Any

from sqlalchemy import func
from sqlalchemy import select
from sqlalchemy.orm import Session

from api.db.models import PermissionGrant
from api.db.models import UUIProxyLink
from api.db.models import UniversalMemory
from api.db.models import UniversalUser
from api.db.vector_store import QdrantService
from api.services.embedding_service import EmbeddingService
from api.services.domain_projection_types import DomainMemoryProjection
from api.services.domain_projection_types import DomainProjectionResult
from api.services.version_service import VersionService


LOGGER = logging.getLogger("memoryos.domain_projection")
ALLOWED_UNIVERSAL_CATEGORIES = {
    "preference",
    "fact",
    "goal",
    "procedure",
    "relationship",
    "expertise",
}


class DomainProjectionService:
    """Projects safe domain facts into universal memory.

    Domain schemas keep their own structured tables. This service only copies
    portable, consent-approved summaries into universal_memories so other
    approved agents can benefit from them.
    """

    UNIVERSAL_COLLECTION_NAME = "universal_memories"

    def __init__(
        self,
        *,
        qdrant_service: QdrantService | None = None,
        embedder: EmbeddingService | None = None,
        version_service: VersionService | None = None,
    ) -> None:
        self.qdrant_service = qdrant_service
        self.embedder = embedder
        self.version_service = version_service

    def project_to_universal_sync(
        self,
        *,
        session: Session,
        tenant_id: str,
        proxy_user_id: str,
        agent_id: str | None,
        projections: list[DomainMemoryProjection],
    ) -> DomainProjectionResult:
        if not projections:
            return DomainProjectionResult()
        if not agent_id:
            return DomainProjectionResult(skipped=len(projections), skipped_reason="missing_agent_id")

        try:
            parsed_tenant_id = uuid.UUID(str(tenant_id))
            parsed_proxy_user_id = uuid.UUID(str(proxy_user_id))
            parsed_agent_id = uuid.UUID(str(agent_id))
        except (TypeError, ValueError):
            return DomainProjectionResult(skipped=len(projections), skipped_reason="invalid_identity")

        link = self._uui_link(
            session=session,
            tenant_id=parsed_tenant_id,
            proxy_user_id=parsed_proxy_user_id,
        )
        if link is None:
            return DomainProjectionResult(skipped=len(projections), skipped_reason="no_uui_link")

        grant = self._active_write_grant(
            session=session,
            user_uui_id=link.user_uui_id,
            agent_id=parsed_agent_id,
        )
        if grant is None:
            return DomainProjectionResult(skipped=len(projections), skipped_reason="write_not_permitted")

        embedder = self.embedder or EmbeddingService(sync_session=session)
        version_service = self.version_service or VersionService(session)
        created = 0
        updated = 0
        unchanged = 0
        skipped = 0

        for projection in projections:
            normalized = self._normalize_projection(projection)
            if normalized is None:
                skipped += 1
                continue

            existing = self._find_existing_projection(
                session=session,
                user_uui_id=link.user_uui_id,
                source_agent_id=parsed_agent_id,
                projection_key=normalized.projection_key,
            )
            if existing is None:
                memory = self._create_universal_memory(
                    session=session,
                    user_uui_id=link.user_uui_id,
                    source_agent_id=parsed_agent_id,
                    projection=normalized,
                )
                session.flush()
                version_service.record_universal_version_sync(
                    memory,
                    "created",
                    f"Projected from {normalized.source_domain} schema",
                    "agent",
                    changed_by_agent_id=str(parsed_agent_id),
                    db_session=session,
                )
                self._upsert_vector(memory, normalized, embedder)
                created += 1
                continue

            if self._projection_matches(existing, normalized):
                unchanged += 1
                continue

            existing.content = normalized.content
            existing.category = normalized.category
            existing.importance_score = normalized.importance_score
            existing.confidence = normalized.confidence
            existing.metadata_json = self._metadata(normalized, previous=existing.metadata_json)
            session.add(existing)
            session.flush()
            version_service.record_universal_version_sync(
                existing,
                "agent_updated",
                f"Updated from {normalized.source_domain} schema projection",
                "agent",
                changed_by_agent_id=str(parsed_agent_id),
                db_session=session,
            )
            self._upsert_vector(existing, normalized, embedder)
            updated += 1

        if created:
            user = session.get(UniversalUser, link.user_uui_id)
            if user is not None:
                user.memory_count = int(user.memory_count or 0) + created
                session.add(user)

        return DomainProjectionResult(
            created=created,
            updated=updated,
            unchanged=unchanged,
            skipped=skipped,
            skipped_reason=None if skipped != len(projections) else "invalid_projection",
        )

    @staticmethod
    def _uui_link(
        *,
        session: Session,
        tenant_id: uuid.UUID,
        proxy_user_id: uuid.UUID,
    ) -> UUIProxyLink | None:
        return session.execute(
            select(UUIProxyLink).where(
                UUIProxyLink.tenant_id == tenant_id,
                UUIProxyLink.proxy_user_id == proxy_user_id,
            )
        ).scalar_one_or_none()

    @staticmethod
    def _active_write_grant(
        *,
        session: Session,
        user_uui_id: uuid.UUID,
        agent_id: uuid.UUID,
    ) -> PermissionGrant | None:
        return session.execute(
            select(PermissionGrant).where(
                PermissionGrant.user_uui_id == user_uui_id,
                PermissionGrant.agent_id == agent_id,
                PermissionGrant.is_active.is_(True),
                PermissionGrant.access_type == "read_write",
                (PermissionGrant.expires_at.is_(None) | (PermissionGrant.expires_at > func.now())),
            )
        ).scalar_one_or_none()

    @staticmethod
    def _find_existing_projection(
        *,
        session: Session,
        user_uui_id: uuid.UUID,
        source_agent_id: uuid.UUID,
        projection_key: str,
    ) -> UniversalMemory | None:
        result = session.execute(
            select(UniversalMemory).where(
                UniversalMemory.user_uui_id == user_uui_id,
                UniversalMemory.source_agent_id == source_agent_id,
                UniversalMemory.is_archived.is_(False),
            )
        )
        for memory in result.scalars().all():
            metadata = memory.metadata_json or {}
            if metadata.get("projection_key") == projection_key:
                return memory
        return None

    @staticmethod
    def _normalize_projection(projection: DomainMemoryProjection) -> DomainMemoryProjection | None:
        content = " ".join((projection.content or "").split())
        category = str(projection.category or "").strip().lower()
        projection_key = str(projection.projection_key or "").strip()
        if not projection_key or len(content) < 10 or category not in ALLOWED_UNIVERSAL_CATEGORIES:
            return None
        return DomainMemoryProjection(
            projection_key=projection_key,
            content=content[:500],
            category=category,
            importance_score=min(10.0, max(1.0, float(projection.importance_score))),
            confidence=min(1.0, max(0.0, float(projection.confidence))),
            source_domain=str(projection.source_domain).strip(),
            source_domain_record_id=str(projection.source_domain_record_id).strip(),
            source_field=str(projection.source_field).strip(),
            portability=str(projection.portability or "cross_agent"),
            sensitivity=str(projection.sensitivity or "normal"),
        )

    @classmethod
    def _metadata(
        cls,
        projection: DomainMemoryProjection,
        *,
        previous: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        metadata = dict(previous or {})
        metadata.update(
            {
                "derived_from_domain": True,
                "source_domain": projection.source_domain,
                "source_domain_record_id": projection.source_domain_record_id,
                "source_field": projection.source_field,
                "projection_key": projection.projection_key,
                "portability": projection.portability,
                "sensitivity": projection.sensitivity,
                "projected_at": datetime.now(UTC).isoformat(),
            }
        )
        return metadata

    def _create_universal_memory(
        self,
        *,
        session: Session,
        user_uui_id: uuid.UUID,
        source_agent_id: uuid.UUID,
        projection: DomainMemoryProjection,
    ) -> UniversalMemory:
        now = datetime.now(UTC)
        memory_id = uuid.uuid4()
        memory = UniversalMemory(
            id=memory_id,
            user_uui_id=user_uui_id,
            source_agent_id=source_agent_id,
            content=projection.content,
            category=projection.category,
            importance_score=projection.importance_score,
            confidence=projection.confidence,
            embedding_id=str(memory_id),
            created_at=now,
            last_accessed_at=now,
            is_archived=False,
            metadata_json=self._metadata(projection),
        )
        session.add(memory)
        return memory

    @classmethod
    def _projection_matches(
        cls,
        memory: UniversalMemory,
        projection: DomainMemoryProjection,
    ) -> bool:
        return (
            memory.content == projection.content
            and memory.category == projection.category
            and abs(float(memory.importance_score) - projection.importance_score) < 0.01
            and abs(float(memory.confidence) - projection.confidence) < 0.01
        )

    def _upsert_vector(
        self,
        memory: UniversalMemory,
        projection: DomainMemoryProjection,
        embedder: EmbeddingService,
    ) -> None:
        try:
            embedding = embedder.embed_sync(projection.content)
            qdrant_service = self.qdrant_service or QdrantService()
            qdrant_service.upsert_memory(
                memory_id=str(memory.id),
                embedding=embedding.vector,
                payload={
                    "memory_id": str(memory.id),
                    "user_uui_id": str(memory.user_uui_id),
                    "source_agent_id": str(memory.source_agent_id),
                    "category": projection.category,
                    "importance_score": projection.importance_score,
                    "is_archived": False,
                    "created_at": (memory.created_at or datetime.now(UTC)).isoformat(),
                    "source_domain": projection.source_domain,
                    "projection_key": projection.projection_key,
                },
                collection_name=self.UNIVERSAL_COLLECTION_NAME,
                vector_size=embedding.dimensions,
            )
        except Exception as exc:
            LOGGER.warning(
                "domain_projection_vector_upsert_failed",
                extra={
                    "event": "domain_projection_vector_upsert_failed",
                    "memory_id": str(memory.id),
                    "projection_key": projection.projection_key,
                    "error": str(exc),
                },
            )


__all__ = [
    "DomainMemoryProjection",
    "DomainProjectionResult",
    "DomainProjectionService",
]
