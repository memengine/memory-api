from __future__ import annotations

import hashlib
import os
import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from api.db.models import ServiceWriter
from api.routers.tenant import _repair_writer_attribution


@pytest.mark.asyncio
async def test_registering_writer_repairs_only_matching_historical_api_key() -> None:
    engine = create_async_engine(os.environ["DATABASE_URL"])
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    tenant_id = uuid.uuid4()
    proxy_user_id = uuid.uuid4()
    matching_key_id = uuid.uuid4()
    other_key_id = uuid.uuid4()
    matching_event_id = uuid.uuid4()
    other_event_id = uuid.uuid4()
    claim_id = uuid.uuid4()
    matching_revision_id = uuid.uuid4()
    other_revision_id = uuid.uuid4()
    writer_id = uuid.uuid4()
    now = datetime.now(UTC)

    async with engine.begin() as connection:
        await connection.execute(text("""
            INSERT INTO tenants (id, company_name, region_id, plan_tier)
            VALUES (:tenant_id, 'Writer repair test', 'IN1', 'starter')
        """), {"tenant_id": tenant_id})
        await connection.execute(text("""
            INSERT INTO proxy_users (id, tenant_id, external_user_id, external_user_id_hash)
            VALUES (:proxy_user_id, :tenant_id, 'writer-repair-user', :user_hash)
        """), {
            "proxy_user_id": proxy_user_id, "tenant_id": tenant_id,
            "user_hash": hashlib.sha256(str(proxy_user_id).encode()).hexdigest(),
        })
        await connection.execute(text("""
            INSERT INTO api_keys (id, tenant_id, key_hash, key_prefix, name, permissions)
            VALUES
              (:matching_key_id, :tenant_id, :matching_hash, 'mem_a001', 'Billing prod', ARRAY['memory:write']),
              (:other_key_id, :tenant_id, :other_hash, 'mem_b001', 'Other prod', ARRAY['memory:write'])
        """), {
            "matching_key_id": matching_key_id, "other_key_id": other_key_id,
            "tenant_id": tenant_id, "matching_hash": "a" * 60, "other_hash": "b" * 60,
        })
        await connection.execute(text("""
            INSERT INTO memory_source_events (
              id, tenant_id, proxy_user_id, api_key_id, source_service,
              source_event_id, observed_at, payload_hash
            ) VALUES
              (:matching_event_id, :tenant_id, :proxy_user_id, :matching_key_id,
               'billing-service', 'billing-match', :now, :matching_payload),
              (:other_event_id, :tenant_id, :proxy_user_id, :other_key_id,
               'billing-service', 'billing-other', :now, :other_payload)
        """), {
            "matching_event_id": matching_event_id, "other_event_id": other_event_id,
            "tenant_id": tenant_id, "proxy_user_id": proxy_user_id,
            "matching_key_id": matching_key_id, "other_key_id": other_key_id,
            "now": now, "matching_payload": "c" * 64, "other_payload": "d" * 64,
        })
        await connection.execute(text("""
            INSERT INTO memory_claims (
              id, tenant_id, proxy_user_id, category, claim_fingerprint,
              subject_key, predicate_key, active_value, status
            ) VALUES (
              :claim_id, :tenant_id, :proxy_user_id, 'fact', :fingerprint,
              'customer', 'subscription_plan', 'growth', 'active'
            )
        """), {
            "claim_id": claim_id, "tenant_id": tenant_id,
            "proxy_user_id": proxy_user_id, "fingerprint": "e" * 64,
        })
        await connection.execute(text("""
            INSERT INTO memory_claim_revisions (
              id, claim_id, source_event_id, asserted_value, status
            ) VALUES
              (:matching_revision_id, :claim_id, :matching_event_id, 'starter', 'superseded'),
              (:other_revision_id, :claim_id, :other_event_id, 'growth', 'activated')
        """), {
            "matching_revision_id": matching_revision_id,
            "other_revision_id": other_revision_id,
            "claim_id": claim_id,
            "matching_event_id": matching_event_id,
            "other_event_id": other_event_id,
        })

    try:
        async with sessions() as session:
            writer = ServiceWriter(
                id=writer_id, tenant_id=tenant_id, api_key_id=matching_key_id,
                service_key="billing-service", display_name="Billing Service",
                authority_rules={}, is_active=True,
            )
            session.add(writer)
            await session.flush()
            repaired = await _repair_writer_attribution(session, writer)
            await session.commit()
            assert repaired == (1, 1)

        async with engine.connect() as connection:
            rows = (await connection.execute(text("""
                SELECT e.id, e.writer_id, r.source_writer_id
                FROM memory_source_events e
                JOIN memory_claim_revisions r ON r.source_event_id = e.id
                WHERE e.id IN (:matching_event_id, :other_event_id)
                ORDER BY e.id
            """), {
                "matching_event_id": matching_event_id, "other_event_id": other_event_id,
            })).mappings().all()
        by_event = {row["id"]: row for row in rows}
        assert by_event[matching_event_id]["writer_id"] == writer_id
        assert by_event[matching_event_id]["source_writer_id"] == writer_id
        assert by_event[other_event_id]["writer_id"] is None
        assert by_event[other_event_id]["source_writer_id"] is None

        async with sessions() as session:
            writer = await session.get(ServiceWriter, writer_id)
            assert await _repair_writer_attribution(session, writer) == (0, 0)
            await session.rollback()
    finally:
        async with engine.begin() as connection:
            await connection.execute(text("DELETE FROM tenants WHERE id = :tenant_id"), {"tenant_id": tenant_id})
        await engine.dispose()