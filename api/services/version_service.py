from __future__ import annotations

import logging
import uuid
from dataclasses import asdict
from dataclasses import dataclass
from typing import Any

from sqlalchemy import func
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from api.db.models import Memory
from api.db.models import MemoryCategory
from api.db.models import MemoryVersion
from api.db.models import ProxyUser


LOGGER = logging.getLogger(__name__)
ALLOWED_CHANGE_TYPES = {
    "created",
    "conflict_update",
    "manual_edit",
    "importance_decay",
    "importance_boost",
    "archived",
}
ALLOWED_CHANGED_BY = {"system", "user", "operator"}


@dataclass(slots=True)
class UserDataExport:
    tenant_id: str
    proxy_user_id: str
    memories: list[dict[str, Any]]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class VersionService:
    def __init__(self, session: Any) -> None:
        self.session = session

    def record_version(
        self,
        memory: Memory,
        change_type: str,
        change_reason: str | None = None,
        changed_by: str = "system",
    ) -> MemoryVersion:
        self._validate_change(change_type=change_type, changed_by=changed_by)
        version = MemoryVersion(
            id=uuid.uuid4(),
            memory_id=memory.id,
            version_number=self._next_version_number(memory.id),
            content=memory.content,
            category=self._category_value(memory.category),
            importance_score=float(memory.importance_score),
            confidence=float(memory.confidence_score),
            change_type=change_type,
            change_reason=change_reason,
            changed_by=changed_by,
        )
        self.session.add(version)
        return version

    async def arecord_version(
        self,
        memory: Memory,
        change_type: str,
        change_reason: str | None = None,
        changed_by: str = "system",
    ) -> MemoryVersion:
        self._validate_change(change_type=change_type, changed_by=changed_by)
        version = MemoryVersion(
            id=uuid.uuid4(),
            memory_id=memory.id,
            version_number=await self._anext_version_number(memory.id),
            content=memory.content,
            category=self._category_value(memory.category),
            importance_score=float(memory.importance_score),
            confidence=float(memory.confidence_score),
            change_type=change_type,
            change_reason=change_reason,
            changed_by=changed_by,
        )
        self.session.add(version)
        return version

    def safe_record_version(
        self,
        memory: Memory,
        change_type: str,
        change_reason: str | None = None,
        changed_by: str = "system",
    ) -> MemoryVersion | None:
        try:
            return self.record_version(memory, change_type, change_reason, changed_by)
        except Exception as exc:
            LOGGER.warning(
                "memory_version_record_failed",
                extra={
                    "event": "memory_version_record_failed",
                    "memory_id": str(getattr(memory, "id", "")),
                    "change_type": change_type,
                    "error": str(exc),
                },
            )
            return None

    async def asafe_record_version(
        self,
        memory: Memory,
        change_type: str,
        change_reason: str | None = None,
        changed_by: str = "system",
    ) -> MemoryVersion | None:
        try:
            return await self.arecord_version(memory, change_type, change_reason, changed_by)
        except Exception as exc:
            LOGGER.warning(
                "memory_version_record_failed",
                extra={
                    "event": "memory_version_record_failed",
                    "memory_id": str(getattr(memory, "id", "")),
                    "change_type": change_type,
                    "error": str(exc),
                },
            )
            return None

    async def get_history(self, memory_id: str, tenant_id: str) -> list[MemoryVersion]:
        await self._verify_memory_tenant(memory_id=memory_id, tenant_id=tenant_id)
        result = await self.session.execute(
            select(MemoryVersion)
            .where(MemoryVersion.memory_id == uuid.UUID(str(memory_id)))
            .order_by(MemoryVersion.version_number.asc())
        )
        return list(result.scalars().all())

    async def get_user_data_export(self, proxy_user_id: str, tenant_id: str) -> UserDataExport:
        proxy_user_uuid = uuid.UUID(str(proxy_user_id))
        tenant_uuid = uuid.UUID(str(tenant_id))
        proxy_user = await self.session.get(ProxyUser, proxy_user_uuid)
        if proxy_user is None or proxy_user.tenant_id != tenant_uuid:
            raise PermissionError("proxy_user_not_found_for_tenant")

        result = await self.session.execute(
            select(Memory)
            .where(Memory.proxy_user_id == proxy_user_uuid)
            .order_by(Memory.created_at.asc(), Memory.id.asc())
        )
        memories = list(result.scalars().all())
        memory_ids = [memory.id for memory in memories]
        versions_by_memory_id: dict[str, list[MemoryVersion]] = {str(memory_id): [] for memory_id in memory_ids}
        if memory_ids:
            version_result = await self.session.execute(
                select(MemoryVersion)
                .where(MemoryVersion.memory_id.in_(memory_ids))
                .order_by(MemoryVersion.memory_id.asc(), MemoryVersion.version_number.asc())
            )
            for version in version_result.scalars().all():
                versions_by_memory_id.setdefault(str(version.memory_id), []).append(version)

        return UserDataExport(
            tenant_id=str(tenant_uuid),
            proxy_user_id=str(proxy_user_uuid),
            memories=[
                {
                    "id": str(memory.id),
                    "content": memory.content,
                    "category": self._category_value(memory.category),
                    "importance_score": float(memory.importance_score),
                    "confidence": float(memory.confidence_score),
                    "is_archived": bool(memory.is_archived),
                    "created_at": memory.created_at.isoformat() if memory.created_at else None,
                    "updated_at": memory.updated_at.isoformat() if memory.updated_at else None,
                    "versions": [
                        self.version_to_dict(version)
                        for version in versions_by_memory_id.get(str(memory.id), [])
                    ],
                }
                for memory in memories
            ],
        )

    async def _verify_memory_tenant(self, *, memory_id: str, tenant_id: str) -> Memory:
        result = await self.session.execute(
            select(Memory)
            .join(ProxyUser, Memory.proxy_user_id == ProxyUser.id)
            .where(
                Memory.id == uuid.UUID(str(memory_id)),
                ProxyUser.tenant_id == uuid.UUID(str(tenant_id)),
            )
        )
        memory = result.scalar_one_or_none()
        if memory is None:
            raise PermissionError("memory_not_found_for_tenant")
        return memory

    def _next_version_number(self, memory_id: uuid.UUID) -> int:
        result = self.session.execute(
            select(func.coalesce(func.max(MemoryVersion.version_number), 0)).where(
                MemoryVersion.memory_id == memory_id
            )
        )
        if hasattr(result, "scalar_one"):
            return int(result.scalar_one() or 0) + 1
        return 1

    async def _anext_version_number(self, memory_id: uuid.UUID) -> int:
        result = await self.session.execute(
            select(func.coalesce(func.max(MemoryVersion.version_number), 0)).where(
                MemoryVersion.memory_id == memory_id
            )
        )
        return int(result.scalar_one() or 0) + 1

    @staticmethod
    def version_to_dict(version: MemoryVersion) -> dict[str, Any]:
        return {
            "id": str(version.id),
            "memory_id": str(version.memory_id),
            "version_number": int(version.version_number),
            "content": version.content,
            "category": version.category,
            "importance_score": float(version.importance_score),
            "confidence": float(version.confidence),
            "change_type": version.change_type,
            "change_reason": version.change_reason,
            "changed_by": version.changed_by,
            "created_at": version.created_at.isoformat() if version.created_at else None,
        }

    @staticmethod
    def _category_value(category: MemoryCategory | str) -> str:
        return category.value if isinstance(category, MemoryCategory) else str(category)

    @staticmethod
    def _validate_change(*, change_type: str, changed_by: str) -> None:
        if change_type not in ALLOWED_CHANGE_TYPES:
            raise ValueError(f"invalid memory version change_type: {change_type}")
        if changed_by not in ALLOWED_CHANGED_BY:
            raise ValueError(f"invalid memory version changed_by: {changed_by}")


__all__ = ["UserDataExport", "VersionService"]
