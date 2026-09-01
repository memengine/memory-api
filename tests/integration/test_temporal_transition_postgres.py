from __future__ import annotations

import asyncio
import hashlib
import os
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from api.services.lifecycle_manager import MemoryLifecycleManager


class _Cache:
    async def set_lifecycle_report(self, *_args, **_kwargs) -> None:
        return None


@pytest.mark.asyncio
async def test_concurrent_expiration_is_atomic_and_idempotent() -> None:
    engine = create_async_engine(os.environ["DATABASE_URL"])
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    tenant_id, user_id, proxy_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    conversation_id, memory_id = uuid.uuid4(), uuid.uuid4()
    claim_id, revision_id = uuid.uuid4(), uuid.uuid4()
    now = datetime(2026, 8, 13, 12, 0, tzinfo=UTC)
    try:
        async with engine.begin() as connection:
            model_id = await connection.scalar(text("SELECT id FROM embedding_models LIMIT 1"))
            assert model_id is not None
            await connection.execute(text("""
                INSERT INTO tenants (id, company_name, region_id, plan_tier)
                VALUES (:tenant, 'Temporal transition test', 'IN1', 'starter')
            """), {"tenant": tenant_id})
            await connection.execute(text("""
                INSERT INTO users (id, external_id, email)
                VALUES (:id, :external, :email)
            """), {
                "id": user_id, "external": f"temporal-{user_id}",
                "email": f"{user_id}@example.invalid",
            })
            await connection.execute(text("""
                INSERT INTO proxy_users (id, tenant_id, external_user_id, external_user_id_hash)
                VALUES (:id, :tenant, 'temporal-transition-user', :hash)
            """), {
                "id": proxy_id, "tenant": tenant_id,
                "hash": hashlib.sha256(str(proxy_id).encode()).hexdigest(),
            })
            await connection.execute(text("""
                INSERT INTO conversations (id, user_id) VALUES (:id, :user)
            """), {"id": conversation_id, "user": user_id})
            await connection.execute(text("""
                INSERT INTO memories (
                    id, user_id, proxy_user_id, content, category,
                    importance_score, confidence_score, embedding_id,
                    embedding_model_id, source_conversation_id, is_archived,
                    effective_from, effective_until, metadata
                ) VALUES (
                    :id, :user, :proxy, 'Temporary office is Pune', 'fact',
                    6, 0.95, :embedding, :model, :conversation, false,
                    :starts, :ends, '{}'::jsonb
                )
            """), {
                "id": memory_id, "user": user_id, "proxy": proxy_id,
                "embedding": str(memory_id), "model": model_id,
                "conversation": conversation_id,
                "starts": now - timedelta(days=30), "ends": now,
            })
            await connection.execute(text("""
                INSERT INTO memory_claims (
                    id, tenant_id, proxy_user_id, category, claim_fingerprint,
                    subject_key, predicate_key, active_value, status,
                    active_memory_id, authority_priority, confidence_score
                ) VALUES (
                    :id, :tenant, :proxy, 'fact', :fingerprint, 'user',
                    'office', 'Pune', 'active', :memory, 50, 0.95
                )
            """), {
                "id": claim_id, "tenant": tenant_id, "proxy": proxy_id,
                "fingerprint": hashlib.sha256(str(claim_id).encode()).hexdigest(),
                "memory": memory_id,
            })
            await connection.execute(text("""
                INSERT INTO memory_claim_revisions (
                    id, claim_id, memory_id, asserted_value, status,
                    authority_priority, confidence_score, evidence_refs,
                    decision_evidence, schema_version, processor_version,
                    effective_from, effective_until
                ) VALUES (
                    :id, :claim, :memory, 'Pune', 'activated', 50, 0.95,
                    '[]'::jsonb, '{}'::jsonb, 1, 'temporal-transition-test',
                    :starts, :ends
                )
            """), {
                "id": revision_id, "claim": claim_id, "memory": memory_id,
                "starts": now - timedelta(days=30), "ends": now,
            })
            await connection.execute(text("""
                UPDATE memory_claims SET winning_revision_id = :revision
                WHERE id = :claim
            """), {"revision": revision_id, "claim": claim_id})

        async def transition() -> tuple[int, int]:
            async with sessions() as session:
                result = await MemoryLifecycleManager(
                    session=session, cache_service=_Cache(), now=now,
                    enforce_off_peak=False,
                )._process_temporal_transitions(
                    tenant_id=tenant_id, reference_time=now
                )
                await session.commit()
                return result

        outcomes = await asyncio.gather(transition(), transition())
        assert sum(expired for _, expired in outcomes) == 1

        async with engine.connect() as connection:
            state = (await connection.execute(text("""
                SELECT m.is_archived, c.status, c.active_memory_id,
                       c.winning_revision_id, r.status
                FROM memories m
                JOIN memory_claim_revisions r ON r.memory_id = m.id
                JOIN memory_claims c ON c.id = r.claim_id
                WHERE m.id = :memory
            """), {"memory": memory_id})).one()
            assert tuple(state) == (True, "archived", None, None, "archived")
            assert await connection.scalar(text("""
                SELECT count(*) FROM vector_sync_outbox WHERE memory_id = :memory
            """), {"memory": memory_id}) == 1
            assert await connection.scalar(text("""
                SELECT count(*) FROM memory_versions
                WHERE memory_id = :memory AND change_type = 'archived'
            """), {"memory": memory_id}) == 1
    finally:
        async with engine.begin() as connection:
            await connection.execute(
                text("DELETE FROM tenants WHERE id = :tenant"), {"tenant": tenant_id}
            )
            await connection.execute(
                text("DELETE FROM users WHERE id = :user"), {"user": user_id}
            )
        await engine.dispose()
