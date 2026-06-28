from __future__ import annotations

import logging
import time as monotonic_time
import uuid
from dataclasses import asdict
from dataclasses import dataclass
from datetime import UTC
from datetime import datetime
from datetime import time
from datetime import timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from api.db.cache import CacheService
from api.db.models import Memory
from api.db.models import ProxyUser
from api.db.vector_store import QdrantService
from api.services.importance_scorer import ImportanceScorer
from api.services.version_service import VersionService


LOGGER = logging.getLogger(__name__)
IST_OFFSET = timedelta(hours=5, minutes=30)
HOT_TIER_TTL_SECONDS = 86400


@dataclass(slots=True)
class LifecycleReport:
    tenant_id: str
    decayed_count: int = 0
    archived_count: int = 0
    promoted_to_hot: int = 0
    rescored_count: int = 0
    skipped: bool = False
    reason: str | None = None
    ran_at: str | None = None
    duration_seconds: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class MemoryLifecycleManager:
    ARCHIVE_IMPORTANCE_THRESHOLD = 1.5
    ARCHIVE_DAYS_THRESHOLD = 90
    HOT_TIER_IMPORTANCE_THRESHOLD = 8.0
    HOT_TIER_ACCESS_THRESHOLD = 5
    HOT_TIER_RECENT_DAYS = 7
    HOT_TIER_TTL = HOT_TIER_TTL_SECONDS

    def __init__(
        self,
        *,
        session: AsyncSession,
        cache_service: CacheService,
        qdrant_service: QdrantService | None = None,
        importance_scorer: ImportanceScorer | None = None,
        now: datetime | None = None,
        enforce_off_peak: bool = True,
    ) -> None:
        self.session = session
        self.cache_service = cache_service
        self.qdrant_service = qdrant_service
        self.importance_scorer = importance_scorer or ImportanceScorer()
        self.now = now
        self.enforce_off_peak = enforce_off_peak

    async def run_for_tenant(self, tenant_id: str) -> LifecycleReport:
        started_at = monotonic_time.perf_counter()
        tenant_uuid = uuid.UUID(str(tenant_id))
        reference_time = self._now()
        report = LifecycleReport(tenant_id=str(tenant_uuid), ran_at=reference_time.isoformat())

        if self.enforce_off_peak and self._is_peak_ist(reference_time):
            report.skipped = True
            report.reason = "peak_hours_ist"
            report.duration_seconds = round(monotonic_time.perf_counter() - started_at, 6)
            await self._store_report(report)
            return report

        report.decayed_count = await self._decay_inactive_memories(
            tenant_id=tenant_uuid,
            reference_time=reference_time,
        )
        report.archived_count = await self._auto_archive_memories(
            tenant_id=tenant_uuid,
            reference_time=reference_time,
        )
        report.promoted_to_hot = await self._promote_hot_memories(
            tenant_id=tenant_uuid,
            reference_time=reference_time,
        )
        report.rescored_count = await self._rescore_baselines(tenant_id=tenant_uuid)

        await self.session.commit()
        report.duration_seconds = round(monotonic_time.perf_counter() - started_at, 6)
        await self._store_report(report)
        LOGGER.info("lifecycle_report", extra={"event": "lifecycle_report", **report.to_dict()})
        return report

    async def _decay_inactive_memories(
        self,
        *,
        tenant_id: uuid.UUID,
        reference_time: datetime,
    ) -> int:
        cutoff = reference_time - timedelta(days=30)
        memories = await self._select_memories(
            tenant_id=tenant_id,
            where=(
                Memory.is_archived.is_(False),
                Memory.last_accessed_at < cutoff,
            ),
        )

        count = 0
        for index, memory in enumerate(memories, start=1):
            old_score = float(memory.importance_score)
            new_score = self.importance_scorer.compute_decay(memory, now=reference_time)
            if old_score != new_score:
                memory.importance_score = new_score
                if abs(old_score - new_score) > 1.0:
                    await VersionService(self.session).asafe_record_version(
                        memory,
                        "importance_decay" if new_score < old_score else "importance_boost",
                        f"Score changed from {old_score:g} to {new_score:g}",
                        "system",
                    )
                self.session.add(memory)
                count += 1
            if index % 200 == 0:
                await self.session.flush()
        return count

    async def _auto_archive_memories(
        self,
        *,
        tenant_id: uuid.UUID,
        reference_time: datetime,
    ) -> int:
        cutoff = reference_time - timedelta(days=self.ARCHIVE_DAYS_THRESHOLD)
        memories = await self._select_memories(
            tenant_id=tenant_id,
            where=(
                Memory.is_archived.is_(False),
                Memory.importance_score < self.ARCHIVE_IMPORTANCE_THRESHOLD,
                Memory.last_accessed_at < cutoff,
                Memory.access_count == 0,
            ),
        )

        archived_count = 0
        for memory in memories:
            memory.is_archived = True
            await VersionService(self.session).asafe_record_version(
                memory,
                "archived",
                "Auto-archived: low importance, no access in 90 days",
                "system",
            )
            self.session.add(memory)
            archived_count += 1
            self._delete_vector(memory)

        if archived_count:
            LOGGER.info(
                "auto_archived",
                extra={
                    "event": "auto_archived",
                    "count": archived_count,
                    "tenant_id": str(tenant_id),
                },
            )
        return archived_count

    async def _promote_hot_memories(
        self,
        *,
        tenant_id: uuid.UUID,
        reference_time: datetime,
    ) -> int:
        cutoff = reference_time - timedelta(days=self.HOT_TIER_RECENT_DAYS)
        memories = await self._select_memories(
            tenant_id=tenant_id,
            where=(
                Memory.is_archived.is_(False),
                Memory.importance_score >= self.HOT_TIER_IMPORTANCE_THRESHOLD,
                Memory.access_count >= self.HOT_TIER_ACCESS_THRESHOLD,
                Memory.last_accessed_at > cutoff,
            ),
        )

        promoted = 0
        for memory in memories:
            await self.cache_service.set_hot_tier_memory(
                str(memory.proxy_user_id),
                str(memory.id),
                self._memory_cache_payload(memory),
                ttl=HOT_TIER_TTL_SECONDS,
            )
            promoted += 1
        return promoted

    async def _rescore_baselines(self, *, tenant_id: uuid.UUID) -> int:
        memories = await self._select_memories(tenant_id=tenant_id, where=())
        for memory in memories:
            self.importance_scorer.recompute_baseline(memory)
            self.session.add(memory)
        return len(memories)

    async def _select_memories(
        self,
        *,
        tenant_id: uuid.UUID,
        where: tuple[Any, ...],
    ) -> list[Memory]:
        stmt = select(Memory).join(ProxyUser, Memory.proxy_user_id == ProxyUser.id).where(
            ProxyUser.tenant_id == tenant_id,
            *where,
        )
        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    def _delete_vector(self, memory: Memory) -> None:
        if self.qdrant_service is None:
            return
        try:
            self.qdrant_service.delete_memory(str(memory.id))
        except Exception as exc:
            LOGGER.warning(
                "qdrant_auto_archive_delete_failed",
                extra={
                    "event": "qdrant_auto_archive_delete_failed",
                    "memory_id": str(memory.id),
                    "error": str(exc),
                },
            )

    async def _store_report(self, report: LifecycleReport) -> None:
        try:
            await self.cache_service.set_lifecycle_report(report.tenant_id, report.to_dict())
        except Exception:
            return

    @staticmethod
    def _memory_cache_payload(memory: Memory) -> dict[str, Any]:
        return {
            "id": str(memory.id),
            "content": memory.content,
            "category": memory.category.value if hasattr(memory.category, "value") else str(memory.category),
            "importance_score": float(memory.importance_score),
            "confidence_score": float(memory.confidence_score),
            "semantic_score": 1.0,
            "recency_score": 1.0,
            "final_score": 1.0,
            "agent_id": str(memory.agent_id) if memory.agent_id else None,
            "previous_version_id": str(memory.previous_version_id) if memory.previous_version_id else None,
            "last_accessed_at": memory.last_accessed_at.isoformat() if memory.last_accessed_at else None,
            "created_at": memory.created_at.isoformat() if memory.created_at else None,
        }

    def _now(self) -> datetime:
        value = self.now or datetime.now(UTC)
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value

    @staticmethod
    def _is_peak_ist(reference_time: datetime) -> bool:
        ist_time = (reference_time.astimezone(UTC) + IST_OFFSET).time()
        return time(9, 0) <= ist_time < time(22, 0)


__all__ = ["LifecycleReport", "MemoryLifecycleManager"]
