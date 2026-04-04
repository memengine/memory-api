from __future__ import annotations

from sqlalchemy import func
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from api.db.models import Agent
from api.db.models import ApiKey
from api.db.models import Memory
from api.services.common import require_user_by_identifier


class UserService:
    def __init__(self, *, session: AsyncSession) -> None:
        self.session = session

    async def get_profile(self, *, authenticated_user_id: str):
        user = await require_user_by_identifier(self.session, authenticated_user_id)
        memory_count = await self._memory_count(user.id)
        storage_bytes = await self._storage_bytes(user.id)
        return user, memory_count, storage_bytes

    async def update_settings(self, *, authenticated_user_id: str, settings: dict):
        user = await require_user_by_identifier(self.session, authenticated_user_id)
        user.settings = settings
        await self.session.commit()
        await self.session.refresh(user)
        memory_count = await self._memory_count(user.id)
        storage_bytes = await self._storage_bytes(user.id)
        return user, memory_count, storage_bytes

    async def export_user_data(self, *, authenticated_user_id: str):
        user = await require_user_by_identifier(self.session, authenticated_user_id)
        memories = list(
            (
                await self.session.execute(
                    select(Memory).where(Memory.user_id == user.id).order_by(Memory.created_at.desc())
                )
            )
            .scalars()
            .all()
        )
        api_keys = list(
            (
                await self.session.execute(
                    select(ApiKey).where(ApiKey.user_id == user.id).order_by(ApiKey.created_at.desc())
                )
            )
            .scalars()
            .all()
        )
        agents = list(
            (
                await self.session.execute(
                    select(Agent).where(Agent.user_id == user.id).order_by(Agent.created_at.desc())
                )
            )
            .scalars()
            .all()
        )
        memory_count = await self._memory_count(user.id)
        storage_bytes = await self._storage_bytes(user.id)
        return user, memory_count, storage_bytes, memories, api_keys, agents

    async def delete_user(self, *, authenticated_user_id: str):
        user = await require_user_by_identifier(self.session, authenticated_user_id)
        memory_count = await self._memory_count(user.id)
        await self.session.delete(user)
        await self.session.commit()
        return True, memory_count

    async def _memory_count(self, user_id) -> int:
        result = await self.session.execute(
            select(func.count(Memory.id)).where(Memory.user_id == user_id)
        )
        return int(result.scalar_one() or 0)

    async def _storage_bytes(self, user_id) -> int:
        result = await self.session.execute(
            select(func.coalesce(func.sum(func.length(Memory.content)), 0)).where(Memory.user_id == user_id)
        )
        return int(result.scalar_one() or 0)
