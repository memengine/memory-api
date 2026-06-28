from __future__ import annotations

import asyncio
import os
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

import pytest
from sqlalchemy import event, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from api.db.models import GlobalAgent
from api.db.models import OrganisationDirectory
from api.db.models import PermissionGrant
from api.db.models import PlanTier
from api.db.models import Tenant
from api.db.models import UniversalMemory
from api.db.models import UniversalMemoryClaim
from api.db.models import UniversalMemoryClaimRevision
from api.db.models import UniversalUser
from api.db.models import VerifiedOrgConnection
from api.services.universal_claim_ledger_service import UniversalClaimLedgerService


@dataclass(slots=True)
class PassportFixture:
    tenant_id: uuid.UUID
    user_a_id: uuid.UUID
    user_b_id: uuid.UUID
    agent_a_id: uuid.UUID
    agent_b_id: uuid.UUID
    grant_a_id: uuid.UUID
    grant_b_id: uuid.UUID
    revoked_grant_id: uuid.UUID
    connection_id: uuid.UUID


async def _create_fixture(session_factory: async_sessionmaker) -> PassportFixture:
    fixture = PassportFixture(
        tenant_id=uuid.uuid4(),
        user_a_id=uuid.uuid4(),
        user_b_id=uuid.uuid4(),
        agent_a_id=uuid.uuid4(),
        agent_b_id=uuid.uuid4(),
        grant_a_id=uuid.uuid4(),
        grant_b_id=uuid.uuid4(),
        revoked_grant_id=uuid.uuid4(),
        connection_id=uuid.uuid4(),
    )
    directory_id = uuid.uuid4()
    now = datetime.now(UTC)
    async with session_factory() as session:
        session.add(
            Tenant(
                id=fixture.tenant_id,
                company_name=f"Phase 2 broad validation {fixture.tenant_id}",
                region_id="IN1",
                plan_tier=PlanTier.starter,
                is_active=True,
                metadata_json={},
                support_type_mode="single",
                support_types_allowed=[],
            )
        )
        session.add_all(
            [
                UniversalUser(
                    id=fixture.user_a_id,
                    uui_token=f"uui_{uuid.uuid4().hex}{uuid.uuid4().hex[:12]}",
                    email=f"phase2-{fixture.user_a_id}@example.test",
                    display_name="Validation User A",
                    created_at=now,
                    is_active=True,
                    memory_count=0,
                ),
                UniversalUser(
                    id=fixture.user_b_id,
                    uui_token=f"uui_{uuid.uuid4().hex}{uuid.uuid4().hex[:12]}",
                    email=f"phase2-{fixture.user_b_id}@example.test",
                    display_name="Validation User B",
                    created_at=now,
                    is_active=True,
                    memory_count=0,
                ),
            ]
        )
        session.add_all(
            [
                GlobalAgent(
                    id=fixture.agent_a_id, owner_tenant_id=fixture.tenant_id,
                    name="Validation Agent A", default_categories_requested=["fact"],
                    redirect_uri="", is_verified=True, is_public=False, is_active=True,
                    created_at=now,
                ),
                GlobalAgent(
                    id=fixture.agent_b_id, owner_tenant_id=fixture.tenant_id,
                    name="Validation Agent B", default_categories_requested=["fact"],
                    redirect_uri="", is_verified=True, is_public=False, is_active=True,
                    created_at=now,
                ),
            ]
        )
        session.add(
            OrganisationDirectory(
                id=directory_id, tenant_id=fixture.tenant_id,
                display_name="Validation Organisation", category="saas",
                oauth_enabled=False, oauth_scopes=[], link_token_enabled=True,
                is_verified=True, is_public=False, created_at=now, updated_at=now,
            )
        )
        await session.flush()
        session.add(
            VerifiedOrgConnection(
                id=fixture.connection_id, user_uui_id=fixture.user_a_id,
                tenant_id=fixture.tenant_id, org_directory_id=directory_id,
                connection_method="link_token", verified_at=now, last_verified_at=now,
                is_active=True,
            )
        )
        session.add_all(
            [
                PermissionGrant(
                    id=fixture.grant_a_id, user_uui_id=fixture.user_a_id,
                    agent_id=fixture.agent_a_id, categories_allowed=["fact"],
                    access_type="read_only", granted_at=now, is_active=True,
                ),
                PermissionGrant(
                    id=fixture.grant_b_id, user_uui_id=fixture.user_a_id,
                    agent_id=fixture.agent_b_id, categories_allowed=["fact"],
                    access_type="read_only", granted_at=now, is_active=True,
                ),
                PermissionGrant(
                    id=fixture.revoked_grant_id, user_uui_id=fixture.user_b_id,
                    agent_id=fixture.agent_a_id, categories_allowed=["fact"],
                    access_type="read_only", granted_at=now, is_active=False,
                    revoked_at=now,
                ),
            ]
        )
        await session.commit()
    return fixture


def _memory(
    *, user_id: uuid.UUID, content: str, source_type: str = "passport_agent",
    agent_id: uuid.UUID | None = None, connection_id: uuid.UUID | None = None,
) -> UniversalMemory:
    return UniversalMemory(
        id=uuid.uuid4(), user_uui_id=user_id, source_agent_id=agent_id,
        source_org_connection_id=connection_id, source_type=source_type,
        content=content, category="fact", importance_score=7.0, confidence=0.95,
        embedding_id=None, created_at=datetime.now(UTC), last_accessed_at=datetime.now(UTC),
        is_archived=False, is_flagged=False, metadata_json={},
    )


async def _record(
    session_factory: async_sessionmaker, memory: UniversalMemory,
    *, grant_id: uuid.UUID | None, tenant_id: uuid.UUID,
) -> uuid.UUID:
    async with session_factory() as session:
        grant = await session.get(PermissionGrant, grant_id) if grant_id else None
        session.add(memory)
        await session.flush()
        await UniversalClaimLedgerService.record_async(
            session, memory, grant=grant, source_tenant_id=tenant_id,
            resolution_reason="phase2 broad validation",
        )
        await session.commit()
    return memory.id


@pytest.mark.asyncio
async def test_phase2_passport_governance_broad_postgres_flow() -> None:
    engine = create_async_engine(os.environ["DATABASE_URL"], pool_size=8, max_overflow=8)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    fixture = await _create_fixture(session_factory)

    try:
        starter = _memory(
            user_id=fixture.user_a_id, agent_id=fixture.agent_a_id,
            content="User's current subscription plan is Starter.",
        )
        growth = _memory(
            user_id=fixture.user_a_id, agent_id=fixture.agent_b_id,
            content="User's current subscription plan is Growth.",
        )
        await asyncio.gather(
            _record(session_factory, starter, grant_id=fixture.grant_a_id, tenant_id=fixture.tenant_id),
            _record(session_factory, growth, grant_id=fixture.grant_b_id, tenant_id=fixture.tenant_id),
        )

        isolated = _memory(
            user_id=fixture.user_b_id, agent_id=fixture.agent_a_id,
            content="User's current subscription plan is Starter.",
        )
        await _record(
            session_factory, isolated, grant_id=fixture.revoked_grant_id,
            tenant_id=fixture.tenant_id,
        )

        correction = _memory(
            user_id=fixture.user_a_id, source_type="user_correction",
            content="User's current subscription plan is Growth.",
        )
        await _record(session_factory, correction, grant_id=None, tenant_id=fixture.tenant_id)

        org_memory = _memory(
            user_id=fixture.user_a_id, source_type="org_connection",
            connection_id=fixture.connection_id,
            content="User's preferred support channel is email.",
        )
        await _record(session_factory, org_memory, grant_id=None, tenant_id=fixture.tenant_id)

        async with session_factory() as session:
            user_a_claims = list((
                await session.execute(
                    select(UniversalMemoryClaim).where(
                        UniversalMemoryClaim.user_uui_id == fixture.user_a_id
                    )
                )
            ).scalars().all())
            user_b_claims = list((
                await session.execute(
                    select(UniversalMemoryClaim).where(
                        UniversalMemoryClaim.user_uui_id == fixture.user_b_id
                    )
                )
            ).scalars().all())
            plan_claim = next(claim for claim in user_a_claims if claim.active_memory_id == correction.id)
            assert len(user_a_claims) == 2
            assert len(user_b_claims) == 1
            assert plan_claim.active_memory_id == correction.id
            assert plan_claim.active_value == "growth"
            assert plan_claim.status == "active"

            concurrent_memories = list((
                await session.execute(
                    select(UniversalMemory).where(UniversalMemory.id.in_([starter.id, growth.id]))
                )
            ).scalars().all())
            assert len(concurrent_memories) == 2
            assert sum(not item.is_archived for item in concurrent_memories) == 1

            revisions = list((
                await session.execute(
                    select(UniversalMemoryClaimRevision).where(
                        UniversalMemoryClaimRevision.claim_id == plan_claim.id
                    )
                )
            ).scalars().all())
            assert len(revisions) == 3
            assert {revision.source_agent_id for revision in revisions if revision.source_agent_id} == {
                fixture.agent_a_id, fixture.agent_b_id
            }
            assert all(revision.source_tenant_id == fixture.tenant_id for revision in revisions)

            observed_ids = [starter.id, growth.id, correction.id, isolated.id, org_memory.id]
            query_count = 0

            def count_query(*_args) -> None:
                nonlocal query_count
                query_count += 1

            event.listen(engine.sync_engine, "before_cursor_execute", count_query)
            started = time.perf_counter()
            provenance = await UniversalClaimLedgerService.provenance_for_memories(
                session, user_uui_id=fixture.user_a_id,
                memory_ids=[starter.id, growth.id, correction.id, org_memory.id],
            )
            elapsed = time.perf_counter() - started
            event.remove(engine.sync_engine, "before_cursor_execute", count_query)

            assert query_count == 1
            assert elapsed < 2.0
            assert provenance[correction.id].grant_status == "not_required"
            assert provenance[org_memory.id].source_type == "org_connection"

            revoked_provenance = await UniversalClaimLedgerService.provenance_for_memories(
                session, user_uui_id=fixture.user_b_id, memory_ids=[isolated.id],
            )
            assert revoked_provenance[isolated.id].grant_status == "revoked"
            assert set(provenance).issubset(set(observed_ids))
    finally:
        async with engine.begin() as connection:
            await connection.execute(
                text("DELETE FROM universal_users WHERE id IN (:user_a_id, :user_b_id)"),
                {"user_a_id": fixture.user_a_id, "user_b_id": fixture.user_b_id},
            )
            await connection.execute(
                text("DELETE FROM tenants WHERE id = :tenant_id"),
                {"tenant_id": fixture.tenant_id},
            )
        await engine.dispose()
