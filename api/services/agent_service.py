from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from api.db.models import Agent
from api.db.models import AgentMemoryScope
from api.services.common import require_user_by_identifier


class AgentService:
    def __init__(self, *, session: AsyncSession) -> None:
        self.session = session

    async def list_agents(
        self,
        *,
        authenticated_user_id: str,
        cursor: str | None,
        limit: int,
    ) -> tuple[list[Agent], str | None, int]:
        user = await require_user_by_identifier(self.session, authenticated_user_id)
        result = await self.session.execute(
            select(Agent)
            .where(Agent.user_id == user.id)
            .order_by(Agent.created_at.desc(), Agent.id.desc())
        )
        agents = list(result.scalars().all())
        total = len(agents)
        sliced = self._slice_with_cursor(agents, cursor=cursor, limit=limit)
        next_cursor = None
        if sliced:
            last_index = agents.index(sliced[-1])
            if (last_index + 1) < len(agents):
                next_cursor = str(sliced[-1].id)
        return sliced, next_cursor, total

    async def create_agent(
        self,
        *,
        authenticated_user_id: str,
        name: str,
        description: str | None,
        memory_scope: str,
    ) -> Agent:
        user = await require_user_by_identifier(self.session, authenticated_user_id)
        agent = Agent(
            id=uuid.uuid4(),
            user_id=user.id,
            name=name,
            description=description,
            memory_scope=AgentMemoryScope(memory_scope),
        )
        self.session.add(agent)
        await self.session.commit()
        await self.session.refresh(agent)
        return agent

    @staticmethod
    def _slice_with_cursor(items: list[Agent], *, cursor: str | None, limit: int) -> list[Agent]:
        if not cursor:
            return items[:limit]
        for index, item in enumerate(items):
            if str(item.id) == cursor:
                return items[index + 1 : index + 1 + limit]
        return items[:limit]
