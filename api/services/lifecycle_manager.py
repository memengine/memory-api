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

from sqlalchemy import or_
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from api.db.cache import CacheService
from api.db.models import Memory
from api.db.models import MemoryClaim
from api.db.models import MemoryClaimRevision
from api.db.models import ProxyUser
from api.db.vector_store import QdrantService
from api.services.importance_scorer import ImportanceScorer
from api.services.vector_outbox import build_vector_payload
from api.services.vector_outbox import enqueue_vector_archive
from api.services.vector_outbox import enqueue_vector_delete
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
    activated_count: int = 0
    expired_count: int = 0
    skipped: bool = False
    reason: str | None = None
    ran_at: str | None = None
    duration_seconds: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class TemporalTransitionReport:
    tenant_id: str
    activated_count: int = 0
    expired_count: int = 0
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

        report.activated_count, report.expired_count = await self._process_temporal_transitions(
            tenant_id=tenant_uuid,
            reference_time=reference_time,
        )
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

    async def run_temporal_transitions_for_tenant(
        self, tenant_id: str
    ) -> TemporalTransitionReport:
        """Process all overdue transitions, including work missed during downtime."""
        started_at = monotonic_time.perf_counter()
        tenant_uuid = uuid.UUID(str(tenant_id))
        reference_time = self._now()
        activated, expired = await self._process_temporal_transitions(
            tenant_id=tenant_uuid,
            reference_time=reference_time,
        )
        await self.session.commit()
        report = TemporalTransitionReport(
            tenant_id=str(tenant_uuid),
            activated_count=activated,
            expired_count=expired,
            ran_at=reference_time.isoformat(),
            duration_seconds=round(monotonic_time.perf_counter() - started_at, 6),
        )
        LOGGER.info(
            "temporal_transition_report",
            extra={"event": "temporal_transition_report", **report.to_dict()},
        )
        return report

    async def _process_temporal_transitions(
        self,
        *,
        tenant_id: uuid.UUID,
        reference_time: datetime,
    ) -> tuple[int, int]:
        """Apply due semantic-validity transitions in the caller's transaction.

        Future rows are activated only when explicitly persisted with
        ``metadata.lifecycle_state=scheduled``. This prevents an archived conflict loser
        from being mistaken for a scheduled value. Expiration may restore only the direct
        predecessor in the same version/claim chain; it never performs a fresh authority
        or conflict decision.
        """
        stmt = (
            select(Memory)
            .join(ProxyUser, Memory.proxy_user_id == ProxyUser.id)
            .where(
                ProxyUser.tenant_id == tenant_id,
                or_(
                    Memory.effective_from <= reference_time,
                    Memory.effective_until <= reference_time,
                ),
            )
            .options(selectinload(Memory.embedding_model))
            .order_by(Memory.id)
            .with_for_update(skip_locked=True)
        )
        result = await self.session.execute(stmt)
        memories = list(result.scalars().all())

        activated = 0
        expired = 0
        # Expire first so an activation due at the same instant sees a vacant winner.
        for memory in memories:
            if (
                not memory.is_archived
                and memory.effective_until is not None
                and memory.effective_until <= reference_time
            ):
                await self._expire_temporal_memory(
                    memory=memory,
                    tenant_id=tenant_id,
                    reference_time=reference_time,
                )
                expired += 1

        for memory in memories:
            metadata = dict(memory.metadata_json or {})
            if (
                memory.is_archived
                and metadata.get("lifecycle_state") == "scheduled"
                and memory.effective_from is not None
                and memory.effective_from <= reference_time
                and (memory.effective_until is None or memory.effective_until > reference_time)
            ):
                await self._activate_temporal_memory(
                    memory=memory,
                    tenant_id=tenant_id,
                    reference_time=reference_time,
                )
                activated += 1
        return activated, expired

    async def _expire_temporal_memory(
        self,
        *,
        memory: Memory,
        tenant_id: uuid.UUID,
        reference_time: datetime,
    ) -> None:
        memory.is_archived = True
        metadata = dict(memory.metadata_json or {})
        metadata["lifecycle_state"] = "expired"
        metadata["lifecycle_transitioned_at"] = reference_time.isoformat()
        memory.metadata_json = metadata
        await VersionService(self.session).asafe_record_version(
            memory, "archived", "Semantic validity interval expired", "system"
        )
        revision, claim = await self._lock_claim_for_memory(memory.id)
        if revision is not None and revision.status == "activated":
            revision.status = "archived"
            self.session.add(revision)
        if claim is not None and claim.active_memory_id == memory.id:
            claim.active_memory_id = None
            claim.winning_revision_id = None
            claim.active_value = None
            claim.status = "archived"
            claim.updated_at = reference_time
            self.session.add(claim)

            predecessor = await self._eligible_predecessor(
                claim_id=claim.id,
                predecessor_memory_id=memory.previous_version_id,
                reference_time=reference_time,
            )
            if predecessor is not None:
                predecessor_memory, predecessor_revision = predecessor
                # PostgreSQL's partial unique index is immediate. Persist the old
                # winner demotion before promoting its predecessor.
                await self.session.flush()
                await self._activate_locked_winner(
                    memory=predecessor_memory,
                    revision=predecessor_revision,
                    claim=claim,
                    tenant_id=tenant_id,
                    reference_time=reference_time,
                    reason="Restored after successor validity expired",
                )
        self.session.add(memory)
        self._enqueue_lifecycle_payload(memory, tenant_id, "expired")

    async def _activate_temporal_memory(
        self,
        *,
        memory: Memory,
        tenant_id: uuid.UUID,
        reference_time: datetime,
    ) -> None:
        revision, claim = await self._lock_claim_for_memory(memory.id)
        if claim is not None and claim.active_memory_id not in {None, memory.id}:
            current_result = await self.session.execute(
                select(Memory)
                .where(Memory.id == claim.active_memory_id)
                .options(selectinload(Memory.embedding_model))
                .with_for_update()
            )
            current = current_result.scalar_one_or_none()
            if current is not None and not current.is_archived:
                current.is_archived = True
                current_metadata = dict(current.metadata_json or {})
                current_metadata["lifecycle_state"] = "superseded"
                current.metadata_json = current_metadata
                current_revision, _ = await self._lock_claim_for_memory(current.id)
                if current_revision is not None and current_revision.status == "activated":
                    current_revision.status = "superseded"
                    self.session.add(current_revision)
                await VersionService(self.session).asafe_record_version(
                    current, "conflict_update", "Superseded by scheduled validity transition", "system"
                )
                self.session.add(current)
                self._enqueue_lifecycle_payload(current, tenant_id, "superseded")
                # Release the claim's unique activated-revision slot before the
                # scheduled revision is promoted.
                await self.session.flush()
        if claim is not None and revision is not None:
            await self._activate_locked_winner(
                memory=memory,
                revision=revision,
                claim=claim,
                tenant_id=tenant_id,
                reference_time=reference_time,
                reason="Scheduled semantic validity began",
            )
        else:
            memory.is_archived = False
            metadata = dict(memory.metadata_json or {})
            metadata["lifecycle_state"] = "active"
            metadata["lifecycle_transitioned_at"] = reference_time.isoformat()
            memory.metadata_json = metadata
            self.session.add(memory)
            self._enqueue_lifecycle_payload(memory, tenant_id, "active")

    async def _activate_locked_winner(
        self,
        *,
        memory: Memory,
        revision: MemoryClaimRevision,
        claim: MemoryClaim,
        tenant_id: uuid.UUID,
        reference_time: datetime,
        reason: str,
    ) -> None:
        memory.is_archived = False
        metadata = dict(memory.metadata_json or {})
        metadata["lifecycle_state"] = "active"
        metadata["lifecycle_transitioned_at"] = reference_time.isoformat()
        memory.metadata_json = metadata
        revision.status = "activated"
        claim.active_memory_id = memory.id
        claim.winning_revision_id = revision.id
        claim.active_value = revision.asserted_value
        claim.status = "active"
        claim.effective_at = reference_time
        claim.updated_at = reference_time
        self.session.add(memory)
        self.session.add(revision)
        self.session.add(claim)
        await VersionService(self.session).asafe_record_version(
            memory, "conflict_resolved", reason, "system"
        )
        self._enqueue_lifecycle_payload(memory, tenant_id, "active")

    async def _lock_claim_for_memory(
        self, memory_id: uuid.UUID
    ) -> tuple[MemoryClaimRevision | None, MemoryClaim | None]:
        revision_result = await self.session.execute(
            select(MemoryClaimRevision)
            .where(MemoryClaimRevision.memory_id == memory_id)
            .order_by(MemoryClaimRevision.created_at.desc())
            .limit(1)
            .with_for_update()
        )
        revision = revision_result.scalars().first()
        if revision is None:
            return None, None
        claim_result = await self.session.execute(
            select(MemoryClaim)
            .where(MemoryClaim.id == revision.claim_id)
            .with_for_update()
        )
        return revision, claim_result.scalar_one_or_none()

    async def _eligible_predecessor(
        self,
        *,
        claim_id: uuid.UUID,
        predecessor_memory_id: uuid.UUID | None,
        reference_time: datetime,
    ) -> tuple[Memory, MemoryClaimRevision] | None:
        if predecessor_memory_id is None:
            return None
        result = await self.session.execute(
            select(Memory, MemoryClaimRevision)
            .join(MemoryClaimRevision, MemoryClaimRevision.memory_id == Memory.id)
            .where(
                Memory.id == predecessor_memory_id,
                MemoryClaimRevision.claim_id == claim_id,
                or_(Memory.effective_from.is_(None), Memory.effective_from <= reference_time),
                or_(Memory.effective_until.is_(None), Memory.effective_until > reference_time),
            )
            .options(selectinload(Memory.embedding_model))
            .with_for_update()
        )
        return result.first()

    def _enqueue_lifecycle_payload(
        self, memory: Memory, tenant_id: uuid.UUID, lifecycle_state: str
    ) -> None:
        payload = build_vector_payload(
            memory,
            tenant_id=str(tenant_id),
            proxy_user_id=str(memory.proxy_user_id),
            user_id=str(memory.user_id),
            embedding_model_id=memory.embedding_model_id,
        )
        payload["is_archived"] = bool(memory.is_archived)
        payload["lifecycle_state"] = lifecycle_state
        enqueue_vector_archive(self.session, memory_id=memory.id, payload=payload)

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
            enqueue_vector_delete(
                self.session,
                memory_id=memory.id,
                payload={"memory_id": str(memory.id)},
            )

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
            "effective_from": memory.effective_from.isoformat() if memory.effective_from else None,
            "effective_until": memory.effective_until.isoformat() if memory.effective_until else None,
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


__all__ = ["LifecycleReport", "MemoryLifecycleManager", "TemporalTransitionReport"]
