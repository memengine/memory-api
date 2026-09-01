from __future__ import annotations

import hashlib
import os
import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import create_async_engine


@pytest.mark.asyncio
async def test_temporal_interval_constraints_exist_and_reject_invalid_claim_revision() -> None:
    engine = create_async_engine(os.environ["DATABASE_URL"])
    tenant_id = uuid.uuid4()
    proxy_user_id = uuid.uuid4()
    claim_id = uuid.uuid4()
    try:
        async with engine.begin() as connection:
            constraints = set(
                (
                    await connection.execute(
                        text(
                            """
                            SELECT conname FROM pg_constraint
                            WHERE conname IN (
                                'ck_memories_effective_interval',
                                'ck_memory_claim_revisions_effective_interval'
                            )
                            """
                        )
                    )
                ).scalars()
            )
            assert constraints == {
                "ck_memories_effective_interval",
                "ck_memory_claim_revisions_effective_interval",
            }
            await connection.execute(
                text(
                    """
                    INSERT INTO tenants (id, company_name, region_id, plan_tier)
                    VALUES (:tenant_id, 'Temporal constraint test', 'IN1', 'starter')
                    """
                ),
                {"tenant_id": tenant_id},
            )
            await connection.execute(
                text(
                    """
                    INSERT INTO proxy_users (
                        id, tenant_id, external_user_id, external_user_id_hash
                    ) VALUES (
                        :proxy_user_id, :tenant_id, 'temporal-constraint-user', :user_hash
                    )
                    """
                ),
                {
                    "proxy_user_id": proxy_user_id,
                    "tenant_id": tenant_id,
                    "user_hash": hashlib.sha256(str(proxy_user_id).encode()).hexdigest(),
                },
            )
            await connection.execute(
                text(
                    """
                    INSERT INTO memory_claims (
                        id, tenant_id, proxy_user_id, category, claim_fingerprint,
                        subject_key, predicate_key, active_value, status,
                        authority_priority, confidence_score
                    ) VALUES (
                        :claim_id, :tenant_id, :proxy_user_id, 'fact', :fingerprint,
                        'user', 'location', 'Jaipur', 'active', 50, 0.9
                    )
                    """
                ),
                {
                    "claim_id": claim_id,
                    "tenant_id": tenant_id,
                    "proxy_user_id": proxy_user_id,
                    "fingerprint": hashlib.sha256(str(claim_id).encode()).hexdigest(),
                },
            )

        with pytest.raises(IntegrityError):
            async with engine.begin() as connection:
                await connection.execute(
                    text(
                        """
                        INSERT INTO memory_claim_revisions (
                            claim_id, asserted_value, status, authority_priority,
                            confidence_score, evidence_refs, decision_evidence,
                            schema_version, processor_version,
                            effective_from, effective_until
                        ) VALUES (
                            :claim_id, 'Jaipur', 'asserted', 50, 0.9,
                            '[]'::jsonb, '{}'::jsonb, 1, 'temporal-regression',
                            '2026-08-13T00:00:00Z', '2026-08-12T00:00:00Z'
                        )
                        """
                    ),
                    {"claim_id": claim_id},
                )
    finally:
        async with engine.begin() as connection:
            await connection.execute(
                text("DELETE FROM tenants WHERE id = :tenant_id"),
                {"tenant_id": tenant_id},
            )
        await engine.dispose()
