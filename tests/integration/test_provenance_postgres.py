from __future__ import annotations

import asyncio
import hashlib
import os
import uuid
from datetime import UTC
from datetime import datetime

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import create_async_engine

from api.tasks.provenance_tasks import redact_expired_extraction_payloads


@pytest.mark.asyncio
async def test_concurrent_source_event_delivery_is_deduplicated() -> None:
    database_url = os.environ["DATABASE_URL"]
    engine = create_async_engine(database_url)
    tenant_id = uuid.uuid4()
    proxy_user_id = uuid.uuid4()
    event_id = f"concurrent-{uuid.uuid4()}"
    payload_hash = hashlib.sha256(b"same-payload").hexdigest()

    async with engine.begin() as connection:
        await connection.execute(
            text(
                """
                INSERT INTO tenants (id, company_name, region_id, plan_tier)
                VALUES (:tenant_id, 'Phase 2 test tenant', 'IN1', 'starter')
                """
            ),
            {"tenant_id": tenant_id},
        )
        await connection.execute(
            text(
                """
                INSERT INTO proxy_users (
                    id, tenant_id, external_user_id, external_user_id_hash
                )
                VALUES (
                    :proxy_user_id, :tenant_id, 'phase2-user', :external_user_id_hash
                )
                """
            ),
            {
                "proxy_user_id": proxy_user_id,
                "tenant_id": tenant_id,
                "external_user_id_hash": hashlib.sha256(b"phase2-user").hexdigest(),
            },
        )

    async def insert_event() -> str:
        try:
            async with engine.begin() as connection:
                await connection.execute(
                    text(
                        """
                        INSERT INTO memory_source_events (
                            tenant_id,
                            proxy_user_id,
                            source_service,
                            source_event_id,
                            observed_at,
                            payload_hash
                        )
                        VALUES (
                            :tenant_id,
                            :proxy_user_id,
                            'billing-service',
                            :source_event_id,
                            :observed_at,
                            :payload_hash
                        )
                        """
                    ),
                    {
                        "tenant_id": tenant_id,
                        "proxy_user_id": proxy_user_id,
                        "source_event_id": event_id,
                        "observed_at": datetime.now(UTC),
                        "payload_hash": payload_hash,
                    },
                )
            return "inserted"
        except IntegrityError:
            return "deduplicated"

    try:
        outcomes = await asyncio.gather(insert_event(), insert_event())
        assert sorted(outcomes) == ["deduplicated", "inserted"]

        async with engine.connect() as connection:
            count = await connection.scalar(
                text(
                    """
                    SELECT count(*)
                    FROM memory_source_events
                    WHERE tenant_id = :tenant_id
                      AND source_service = 'billing-service'
                      AND source_event_id = :source_event_id
                    """
                ),
                {"tenant_id": tenant_id, "source_event_id": event_id},
            )
        assert count == 1
    finally:
        async with engine.begin() as connection:
            await connection.execute(
                text("DELETE FROM tenants WHERE id = :tenant_id"),
                {"tenant_id": tenant_id},
            )
        await engine.dispose()


@pytest.mark.asyncio
async def test_expired_extraction_payloads_are_redacted_in_database() -> None:
    database_url = os.environ["DATABASE_URL"]
    engine = create_async_engine(database_url)
    tenant_id = uuid.uuid4()
    proxy_user_id = uuid.uuid4()
    job_id = uuid.uuid4()

    async with engine.begin() as connection:
        await connection.execute(
            text(
                """
                INSERT INTO tenants (id, company_name, region_id, plan_tier)
                VALUES (:tenant_id, 'Retention test tenant', 'IN1', 'starter')
                """
            ),
            {"tenant_id": tenant_id},
        )
        await connection.execute(
            text(
                """
                INSERT INTO proxy_users (
                    id, tenant_id, external_user_id, external_user_id_hash
                )
                VALUES (
                    :proxy_user_id, :tenant_id, 'retention-user', :external_user_id_hash
                )
                """
            ),
            {
                "proxy_user_id": proxy_user_id,
                "tenant_id": tenant_id,
                "external_user_id_hash": hashlib.sha256(b"retention-user").hexdigest(),
            },
        )
        await connection.execute(
            text(
                """
                INSERT INTO extraction_jobs (
                    id,
                    tenant_id,
                    proxy_user_id,
                    external_user_id,
                    payload,
                    result,
                    raw_payload_expires_at
                )
                VALUES (
                    :job_id,
                    :tenant_id,
                    :proxy_user_id,
                    'retention-user',
                    CAST(:payload AS jsonb),
                    CAST(:result AS jsonb),
                    :expires_at
                )
                """
            ),
            {
                "job_id": job_id,
                "tenant_id": tenant_id,
                "proxy_user_id": proxy_user_id,
                "payload": '{"job_id":"retention-job","messages":[{"role":"user","content":"private"}]}',
                "result": '{"status":"processed","messages":[{"role":"user","content":"private"}]}',
                "expires_at": datetime(2026, 1, 1, tzinfo=UTC),
            },
        )

    try:
        result = await asyncio.to_thread(redact_expired_extraction_payloads.run, 1000)
        assert result["redacted_jobs"] >= 1

        async with engine.connect() as connection:
            row = (
                await connection.execute(
                    text(
                        """
                        SELECT payload, result, payload_redacted_at
                        FROM extraction_jobs
                        WHERE id = :job_id
                        """
                    ),
                    {"job_id": job_id},
                )
            ).one()
        assert "messages" not in row.payload
        assert row.payload["messages_redacted"] is True
        assert "messages" not in row.result
        assert row.result["messages_redacted"] is True
        assert row.payload_redacted_at is not None
    finally:
        async with engine.begin() as connection:
            await connection.execute(
                text("DELETE FROM tenants WHERE id = :tenant_id"),
                {"tenant_id": tenant_id},
            )
        await engine.dispose()
