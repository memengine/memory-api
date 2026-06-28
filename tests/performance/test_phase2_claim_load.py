from __future__ import annotations

import asyncio
import json
import os
import statistics
import time
import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from api.db.models import GlobalAgent
from api.db.models import PermissionGrant
from api.db.models import PlanTier
from api.db.models import Tenant
from api.db.models import UniversalMemory
from api.db.models import UniversalMemoryClaim
from api.db.models import UniversalMemoryClaimRevision
from api.db.models import UniversalUser
from api.services.universal_claim_ledger_service import UniversalClaimLedgerService


def _percentile(values: list[float], percentile: float) -> float:
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int(round((len(ordered) - 1) * percentile))))
    return ordered[index]


def _memory(user_id: uuid.UUID, agent_id: uuid.UUID, plan: str) -> UniversalMemory:
    now = datetime.now(UTC)
    return UniversalMemory(
        id=uuid.uuid4(), user_uui_id=user_id, source_agent_id=agent_id,
        source_type="passport_agent",
        content=f"User's current subscription plan is {plan}.", category="fact",
        importance_score=7.0, confidence=0.95, embedding_id=None,
        created_at=now, last_accessed_at=now, is_archived=False,
        is_flagged=False, metadata_json={},
    )


@pytest.mark.asyncio
async def test_phase2_claim_ledger_concurrent_load() -> None:
    user_count = int(os.getenv("PHASE2_LOAD_USERS", "1000"))
    concurrency = int(os.getenv("PHASE2_LOAD_CONCURRENCY", "32"))
    p95_budget_ms = float(os.getenv("PHASE2_WRITE_P95_MS", "2000"))
    run_id = uuid.uuid4().hex
    tenant_id = uuid.uuid4()
    agent_ids = [uuid.uuid4(), uuid.uuid4()]
    engine = create_async_engine(
        os.environ["DATABASE_URL"], pool_size=concurrency, max_overflow=8, pool_pre_ping=True,
    )
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    users = [(uuid.uuid4(), uuid.uuid4(), uuid.uuid4()) for _ in range(user_count)]
    now = datetime.now(UTC)

    async with sessions() as session:
        session.add(Tenant(
            id=tenant_id, company_name=f"Phase 2 load {run_id}", region_id="IN1",
            plan_tier=PlanTier.starter, is_active=True, metadata_json={},
            support_type_mode="single", support_types_allowed=[],
        ))
        session.add_all([
            GlobalAgent(
                id=agent_id, owner_tenant_id=tenant_id, name=f"Load agent {index}",
                default_categories_requested=["fact"], redirect_uri="",
                is_verified=True, is_public=False, is_active=True, created_at=now,
            )
            for index, agent_id in enumerate(agent_ids)
        ])
        await session.flush()
        for index, (user_id, grant_a, grant_b) in enumerate(users):
            session.add(UniversalUser(
                id=user_id, uui_token=f"uui_{uuid.uuid4().hex}{uuid.uuid4().hex[:12]}",
                email=f"phase2-load-{run_id}-{index}@example.test", display_name="Load user",
                created_at=now, is_active=True, memory_count=0,
            ))
            session.add_all([
                PermissionGrant(
                    id=grant_a, user_uui_id=user_id, agent_id=agent_ids[0],
                    categories_allowed=["fact"], access_type="read_only",
                    granted_at=now, is_active=True,
                ),
                PermissionGrant(
                    id=grant_b, user_uui_id=user_id, agent_id=agent_ids[1],
                    categories_allowed=["fact"], access_type="read_only",
                    granted_at=now, is_active=True,
                ),
            ])
        await session.commit()

    semaphore = asyncio.Semaphore(concurrency)
    latencies_ms: list[float] = []

    async def write(user_id: uuid.UUID, grant_id: uuid.UUID, agent_id: uuid.UUID, plan: str) -> None:
        async with semaphore, sessions() as session:
            started = time.perf_counter()
            grant = await session.get(PermissionGrant, grant_id)
            memory = _memory(user_id, agent_id, plan)
            session.add(memory)
            await session.flush()
            await UniversalClaimLedgerService.record_async(
                session, memory, grant=grant, source_tenant_id=tenant_id,
                resolution_reason="phase2 load validation",
            )
            await session.commit()
            latencies_ms.append((time.perf_counter() - started) * 1000)

    started = time.perf_counter()
    try:
        await asyncio.gather(*[
            write(user_id, grant_id, agent_id, plan)
            for user_id, grant_a, grant_b in users
            for grant_id, agent_id, plan in (
                (grant_a, agent_ids[0], "Starter"),
                (grant_b, agent_ids[1], "Growth"),
            )
        ])
        elapsed = time.perf_counter() - started

        async with sessions() as session:
            claim_count = int((await session.execute(
                select(func.count(UniversalMemoryClaim.id)).where(
                    UniversalMemoryClaim.user_uui_id.in_([item[0] for item in users])
                )
            )).scalar_one())
            revision_count = int((await session.execute(
                select(func.count(UniversalMemoryClaimRevision.id))
                .join(UniversalMemoryClaim)
                .where(UniversalMemoryClaim.user_uui_id.in_([item[0] for item in users]))
            )).scalar_one())

        p50 = _percentile(latencies_ms, 0.50)
        p95 = _percentile(latencies_ms, 0.95)
        p99 = _percentile(latencies_ms, 0.99)
        print(json.dumps({
            "users": user_count, "writes": len(latencies_ms), "concurrency": concurrency,
            "elapsed_seconds": round(elapsed, 3),
            "throughput_writes_per_second": round(len(latencies_ms) / elapsed, 2),
            "write_latency_ms": {
                "mean": round(statistics.fmean(latencies_ms), 2),
                "p50": round(p50, 2), "p95": round(p95, 2), "p99": round(p99, 2),
            },
            "claims": claim_count, "revisions": revision_count,
        }, sort_keys=True))

        assert claim_count == user_count
        assert revision_count == user_count * 2
        assert p95 < p95_budget_ms

        # pool_pre_ping must recover cleanly after all pooled connections are discarded.
        await engine.dispose()
        async with sessions() as session:
            assert (await session.execute(text("SELECT 1"))).scalar_one() == 1
    finally:
        async with engine.begin() as connection:
            await connection.execute(
                text("DELETE FROM universal_users WHERE email LIKE :prefix"),
                {"prefix": f"phase2-load-{run_id}-%"},
            )
            await connection.execute(
                text("DELETE FROM tenants WHERE id = :tenant_id"), {"tenant_id": tenant_id},
            )
        await engine.dispose()