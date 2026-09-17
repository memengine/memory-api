from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from api.db.models import MemoryProposal
from api.errors import APIError


@dataclass(frozen=True, slots=True)
class ProposalTarget:
    id: uuid.UUID
    tenant_id: uuid.UUID
    proxy_user_id: uuid.UUID
    conversation_scope_id: str
    group_id: str
    ordinal: int
    assistant_turn_id: str


class ProposalWindowService:
    """Deterministic proposal ownership and lifecycle; no semantic interpretation."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def active_group(
        self,
        *,
        tenant_id: uuid.UUID,
        proxy_user_id: uuid.UUID,
        conversation_scope_id: str,
        now: datetime | None = None,
    ) -> list[ProposalTarget]:
        at = now or datetime.now(UTC)
        await self.session.execute(
            update(MemoryProposal)
            .where(
                MemoryProposal.tenant_id == tenant_id,
                MemoryProposal.proxy_user_id == proxy_user_id,
                MemoryProposal.conversation_scope_id == conversation_scope_id,
                MemoryProposal.status == "active",
                MemoryProposal.expires_at <= at,
            )
            .values(status="expired", resolved_at=at)
        )
        rows = (
            await self.session.execute(
                select(MemoryProposal)
                .where(
                    MemoryProposal.tenant_id == tenant_id,
                    MemoryProposal.proxy_user_id == proxy_user_id,
                    MemoryProposal.conversation_scope_id == conversation_scope_id,
                    MemoryProposal.status == "active",
                    MemoryProposal.expires_at > at,
                )
                .order_by(MemoryProposal.created_at.desc(), MemoryProposal.proposal_ordinal.asc())
            )
        ).scalars().all()
        if not rows:
            return []
        latest_group = rows[0].proposal_group_id
        return [
            ProposalTarget(
                id=row.id,
                tenant_id=row.tenant_id,
                proxy_user_id=row.proxy_user_id,
                conversation_scope_id=row.conversation_scope_id,
                group_id=row.proposal_group_id,
                ordinal=row.proposal_ordinal,
                assistant_turn_id=row.assistant_turn_id,
            )
            for row in rows
            if row.proposal_group_id == latest_group
        ]

    async def resolve_explicit_target(
        self,
        *,
        tenant_id: uuid.UUID,
        proxy_user_id: uuid.UUID,
        conversation_scope_id: str,
        proposal_id: uuid.UUID | None = None,
        ordinal: int | None = None,
        now: datetime | None = None,
    ) -> ProposalTarget:
        if (proposal_id is None) == (ordinal is None):
            raise APIError(status_code=400, code="PROP_400", error="one_proposal_reference_required")
        active = await self.active_group(
            tenant_id=tenant_id,
            proxy_user_id=proxy_user_id,
            conversation_scope_id=conversation_scope_id,
            now=now,
        )
        matches = [
            proposal for proposal in active
            if proposal.id == proposal_id or proposal.ordinal == ordinal
        ]
        if len(matches) != 1:
            raise APIError(status_code=409, code="PROP_409", error="proposal_reference_not_active")
        return matches[0]

    async def mark_resolved(
        self,
        *,
        target: ProposalTarget,
        status: str,
        now: datetime | None = None,
    ) -> None:
        if status not in {"accepted", "cancelled"}:
            raise ValueError("Proposal resolution status must be accepted or cancelled.")
        at = now or datetime.now(UTC)
        result = await self.session.execute(
            update(MemoryProposal)
            .where(
                MemoryProposal.id == target.id,
                MemoryProposal.tenant_id == target.tenant_id,
                MemoryProposal.proxy_user_id == target.proxy_user_id,
                MemoryProposal.conversation_scope_id == target.conversation_scope_id,
                MemoryProposal.status == "active",
                MemoryProposal.expires_at > at,
            )
            .values(status=status, resolved_at=at)
        )
        if result.rowcount != 1:
            raise APIError(status_code=409, code="PROP_409", error="proposal_already_resolved")
        if status == "accepted":
            await self.session.execute(
                update(MemoryProposal)
                .where(
                    MemoryProposal.proposal_group_id == target.group_id,
                    MemoryProposal.tenant_id == target.tenant_id,
                    MemoryProposal.proxy_user_id == target.proxy_user_id,
                    MemoryProposal.conversation_scope_id == target.conversation_scope_id,
                    MemoryProposal.id != target.id,
                    MemoryProposal.status == "active",
                )
                .values(status="cancelled", resolved_at=at)
            )


__all__ = ["ProposalTarget", "ProposalWindowService"]
