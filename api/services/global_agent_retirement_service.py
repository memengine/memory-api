from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from api.db.models import AgentApiKey, GlobalAgent, PermissionGrant


@dataclass(frozen=True, slots=True)
class GlobalAgentRetirementResult:
    agent: GlobalAgent
    revoked_api_keys: int
    revoked_grants: int


class GlobalAgentRetirementService:
    """Retire an agent without destroying its provenance identity."""

    def __init__(self, *, session: AsyncSession) -> None:
        self.session = session

    async def retire(
        self, *, tenant_id: str, agent_id: str
    ) -> GlobalAgentRetirementResult | None:
        result = await self.session.execute(
            select(GlobalAgent)
            .where(
                GlobalAgent.id == uuid.UUID(str(agent_id)),
                GlobalAgent.owner_tenant_id == uuid.UUID(str(tenant_id)),
            )
            .with_for_update()
        )
        agent = result.scalar_one_or_none()
        if agent is None:
            return None

        key_rows = list(
            (
                await self.session.execute(
                    select(AgentApiKey).where(
                        AgentApiKey.global_agent_id == agent.id,
                        AgentApiKey.is_active.is_(True),
                    )
                )
            )
            .scalars()
            .all()
        )
        grant_rows = list(
            (
                await self.session.execute(
                    select(PermissionGrant).where(
                        PermissionGrant.agent_id == agent.id,
                        PermissionGrant.is_active.is_(True),
                    )
                )
            )
            .scalars()
            .all()
        )
        retired_at = datetime.now(UTC)
        agent.is_active = False
        for key in key_rows:
            key.is_active = False
        for grant in grant_rows:
            grant.is_active = False
            grant.revoked_at = retired_at

        await self.session.commit()
        await self.session.refresh(agent)
        return GlobalAgentRetirementResult(
            agent=agent,
            revoked_api_keys=len(key_rows),
            revoked_grants=len(grant_rows),
        )
