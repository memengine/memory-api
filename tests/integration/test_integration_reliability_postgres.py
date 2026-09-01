from __future__ import annotations

import asyncio
import concurrent.futures
import hashlib
import os
import uuid
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from unittest.mock import MagicMock

import pytest
from sqlalchemy import create_engine
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import create_async_engine

from api.tasks import watchdog_tasks


@pytest.mark.asyncio
async def test_cross_user_conflict_unordered_memory_pair_is_unique() -> None:
    engine = create_async_engine(os.environ["DATABASE_URL"])
    tenant_id = uuid.uuid4()
    user_ids = [uuid.uuid4(), uuid.uuid4()]
    proxy_ids = [uuid.uuid4(), uuid.uuid4()]
    conversation_ids = [uuid.uuid4(), uuid.uuid4()]
    memory_ids = [uuid.uuid4(), uuid.uuid4()]
    try:
        async with engine.begin() as connection:
            await connection.execute(
                text("""
                    INSERT INTO tenants (id, company_name, region_id, plan_tier)
                    VALUES (:tenant_id, 'Conflict pair uniqueness test', 'IN1', 'starter')
                """),
                {"tenant_id": tenant_id},
            )
            for index in range(2):
                await connection.execute(
                    text("""
                        INSERT INTO users (id, external_id, email)
                        VALUES (:id, :external_id, :email)
                    """),
                    {
                        "id": user_ids[index],
                        "external_id": f"conflict-pair-{user_ids[index]}",
                        "email": f"{user_ids[index]}@example.invalid",
                    },
                )
                await connection.execute(
                    text("""
                        INSERT INTO proxy_users (
                            id, tenant_id, external_user_id, external_user_id_hash
                        ) VALUES (:id, :tenant_id, :external_user_id, :user_hash)
                    """),
                    {
                        "id": proxy_ids[index],
                        "tenant_id": tenant_id,
                        "external_user_id": f"conflict-pair-proxy-{index}",
                        "user_hash": hashlib.sha256(str(proxy_ids[index]).encode()).hexdigest(),
                    },
                )
                await connection.execute(
                    text("INSERT INTO conversations (id, user_id) VALUES (:id, :user_id)"),
                    {"id": conversation_ids[index], "user_id": user_ids[index]},
                )
                await connection.execute(
                    text("""
                        INSERT INTO memories (
                            id, user_id, proxy_user_id, content, category,
                            importance_score, confidence_score, embedding_id,
                            embedding_model_id, source_conversation_id, metadata
                        ) VALUES (
                            :id, :user_id, :proxy_user_id, :content, 'fact',
                            6, 0.9, :embedding_id, 'gemini-embedding-001-v1',
                            :conversation_id, '{}'::jsonb
                        )
                    """),
                    {
                        "id": memory_ids[index],
                        "user_id": user_ids[index],
                        "proxy_user_id": proxy_ids[index],
                        "content": f"Stack value {index}",
                        "embedding_id": str(memory_ids[index]),
                        "conversation_id": conversation_ids[index],
                    },
                )
            await connection.execute(
                text("""
                    INSERT INTO cross_user_conflicts (
                        tenant_id, user_a_memory_id, user_b_memory_id,
                        entity_type, entity_value_a, entity_value_b
                    ) VALUES (
                        :tenant_id, :memory_a, :memory_b,
                        'tech_stack', 'python', 'go'
                    )
                """),
                {
                    "tenant_id": tenant_id,
                    "memory_a": memory_ids[0],
                    "memory_b": memory_ids[1],
                },
            )

        with pytest.raises(IntegrityError):
            async with engine.begin() as connection:
                await connection.execute(
                    text("""
                        INSERT INTO cross_user_conflicts (
                            tenant_id, user_a_memory_id, user_b_memory_id,
                            entity_type, entity_value_a, entity_value_b
                        ) VALUES (
                            :tenant_id, :memory_b, :memory_a,
                            'tech_stack', 'go', 'python'
                        )
                    """),
                    {
                        "tenant_id": tenant_id,
                        "memory_a": memory_ids[0],
                        "memory_b": memory_ids[1],
                    },
                )
    finally:
        async with engine.begin() as connection:
            await connection.execute(
                text("DELETE FROM tenants WHERE id = :tenant_id"),
                {"tenant_id": tenant_id},
            )
            await connection.execute(
                text("DELETE FROM users WHERE id = ANY(:user_ids)"),
                {"user_ids": user_ids},
            )
        await engine.dispose()


@pytest.mark.asyncio
async def test_concurrent_claim_revision_updates_leave_one_winner() -> None:
    """Two transactions cannot commit activated revisions for one claim."""
    engine = create_async_engine(os.environ["DATABASE_URL"])
    tenant_id = uuid.uuid4()
    proxy_user_id = uuid.uuid4()
    claim_id = uuid.uuid4()
    try:
        async with engine.begin() as connection:
            await connection.execute(text("""
                INSERT INTO tenants (id, company_name, region_id, plan_tier)
                VALUES (:tenant_id, 'Claim concurrency test', 'IN1', 'starter')
            """), {"tenant_id": tenant_id})
            await connection.execute(text("""
                INSERT INTO proxy_users (id, tenant_id, external_user_id, external_user_id_hash)
                VALUES (:proxy_user_id, :tenant_id, 'claim-race-user', :user_hash)
            """), {
                "proxy_user_id": proxy_user_id, "tenant_id": tenant_id,
                "user_hash": hashlib.sha256(str(proxy_user_id).encode()).hexdigest(),
            })
            await connection.execute(text("""
                INSERT INTO memory_claims (
                    id, tenant_id, proxy_user_id, category, claim_fingerprint,
                    subject_key, predicate_key, active_value, status,
                    authority_priority, confidence_score
                ) VALUES (
                    :claim_id, :tenant_id, :proxy_user_id, 'fact', :fingerprint,
                    'user', 'current plan', 'Starter', 'active', 50, 0.9
                )
            """), {
                "claim_id": claim_id, "tenant_id": tenant_id,
                "proxy_user_id": proxy_user_id,
                "fingerprint": hashlib.sha256(str(claim_id).encode()).hexdigest(),
            })

        gate = asyncio.Event()

        async def insert_activated(value: str) -> str:
            await gate.wait()
            try:
                async with engine.begin() as connection:
                    await connection.execute(text("""
                        INSERT INTO memory_claim_revisions (
                            claim_id, asserted_value, status, authority_priority,
                            confidence_score, evidence_refs, decision_evidence,
                            schema_version, processor_version
                        ) VALUES (
                            :claim_id, :value, 'activated', 50, 0.9,
                            '[]'::jsonb, '{}'::jsonb, 1, 'concurrency-regression'
                        )
                    """), {"claim_id": claim_id, "value": value})
                return "committed"
            except IntegrityError:
                return "rejected"

        tasks = [
            asyncio.create_task(insert_activated("Growth")),
            asyncio.create_task(insert_activated("Enterprise")),
        ]
        gate.set()
        outcomes = await asyncio.gather(*tasks)
        assert sorted(outcomes) == ["committed", "rejected"]

        async with engine.connect() as connection:
            activated = int(await connection.scalar(text("""
                SELECT count(*) FROM memory_claim_revisions
                WHERE claim_id = :claim_id AND status = 'activated'
            """), {"claim_id": claim_id}) or 0)
        assert activated == 1
    finally:
        async with engine.begin() as connection:
            await connection.execute(
                text("DELETE FROM tenants WHERE id = :tenant_id"),
                {"tenant_id": tenant_id},
            )
        await engine.dispose()


def test_concurrent_watchdogs_dispatch_stranded_queued_job_once(monkeypatch) -> None:
    sync_url = os.environ["DATABASE_URL"].replace("postgresql+asyncpg://", "postgresql://", 1)
    engine = create_engine(sync_url)
    tenant_id = uuid.uuid4()
    proxy_user_id = uuid.uuid4()
    job_id = uuid.uuid4()
    apply_async = MagicMock()
    monkeypatch.setattr(watchdog_tasks.process_extraction_job, "apply_async", apply_async)
    try:
        with engine.begin() as connection:
            connection.execute(text("""
                INSERT INTO tenants (id, company_name, region_id, plan_tier)
                VALUES (:tenant_id, 'Queued dispatch recovery test', 'IN1', 'starter')
            """), {"tenant_id": tenant_id})
            connection.execute(text("""
                INSERT INTO proxy_users (id, tenant_id, external_user_id, external_user_id_hash)
                VALUES (:proxy_user_id, :tenant_id, :external_user_id, :user_hash)
            """), {
                "proxy_user_id": proxy_user_id,
                "tenant_id": tenant_id,
                "external_user_id": f"queued-recovery-{job_id}",
                "user_hash": hashlib.sha256(str(proxy_user_id).encode()).hexdigest(),
            })
            connection.execute(text("""
                INSERT INTO extraction_jobs (
                    id, tenant_id, proxy_user_id, external_user_id, status,
                    queue_name, payload, queued_at, created_at, updated_at
                ) VALUES (
                    :job_id, :tenant_id, :proxy_user_id, :external_user_id, 'queued',
                    'starter-extraction', CAST(:payload AS jsonb), :queued_at, :queued_at, :queued_at
                )
            """), {
                "job_id": job_id,
                "tenant_id": tenant_id,
                "proxy_user_id": proxy_user_id,
                "external_user_id": f"queued-recovery-{job_id}",
                "payload": '{"job_id": "' + str(job_id) + '"}',
                "queued_at": datetime.now(UTC) - timedelta(minutes=5),
            })

        def run_cycle() -> dict[str, int]:
            return watchdog_tasks.run_watchdog_cycle(
                session_factory=lambda: watchdog_tasks.Session(bind=engine)
            )

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(lambda _index: run_cycle(), range(2)))

        assert sum(result["requeued"] for result in results) == 1
        apply_async.assert_called_once()
        with engine.connect() as connection:
            row = connection.execute(text("""
                SELECT status::text, celery_task_id
                FROM extraction_jobs WHERE id = :job_id
            """), {"job_id": job_id}).one()
        assert row.status == "queued"
        assert row.celery_task_id
    finally:
        with engine.begin() as connection:
            connection.execute(text("DELETE FROM tenants WHERE id = :tenant_id"), {"tenant_id": tenant_id})
        engine.dispose()
