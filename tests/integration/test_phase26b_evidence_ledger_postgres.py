from __future__ import annotations

import hashlib
import os
import uuid
from unittest.mock import MagicMock

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker

from api.errors import APIError
from api.services.memory_service import MemoryService


@pytest.mark.asyncio
async def test_evidence_and_proposal_scope_constraints_execute_in_postgres() -> None:
    database_url = os.getenv("DATABASE_URL")
    if not database_url:
        pytest.skip("DATABASE_URL is required for PostgreSQL integration coverage")

    engine = create_async_engine(database_url)
    tenant_id = uuid.uuid4()
    proxy_user_id = uuid.uuid4()
    extraction_job_id = uuid.uuid4()
    evidence_id = uuid.uuid4()
    try:
        async with engine.begin() as connection:
            await connection.execute(
                text("""
                    INSERT INTO tenants (id, company_name, region_id, plan_tier)
                    VALUES (:id, 'Phase 2.6b evidence test', 'IN1', 'starter')
                """),
                {"id": tenant_id},
            )
            await connection.execute(
                text("""
                    INSERT INTO proxy_users (id, tenant_id, external_user_id, external_user_id_hash)
                    VALUES (:id, :tenant_id, 'phase26b-user', :external_user_id_hash)
                """),
                {
                    "id": proxy_user_id,
                    "tenant_id": tenant_id,
                    "external_user_id_hash": hashlib.sha256(b"phase26b-user").hexdigest(),
                },
            )
            await connection.execute(
                text("""
                    INSERT INTO extraction_jobs (id, tenant_id, proxy_user_id, external_user_id, status, payload, result)
                    VALUES (:id, :tenant_id, :proxy_user_id, 'phase26b-user', 'queued', '{}'::jsonb, '{}'::jsonb)
                """),
                {"id": extraction_job_id, "tenant_id": tenant_id, "proxy_user_id": proxy_user_id},
            )
            await connection.execute(
                text("""
                    INSERT INTO conversation_evidence_turns (
                        id, tenant_id, proxy_user_id, extraction_job_id, conversation_scope_id,
                        turn_id, role, source_kind, content_sha256
                    ) VALUES (
                        :id, :tenant_id, :proxy_user_id, :extraction_job_id, 'external:chat-26b',
                        'external:turn-1', 'assistant', 'assistant_output', :content_sha256
                    )
                """),
                {
                    "id": evidence_id,
                    "tenant_id": tenant_id,
                    "proxy_user_id": proxy_user_id,
                    "extraction_job_id": extraction_job_id,
                    "content_sha256": hashlib.sha256(b"proposal").hexdigest(),
                },
            )
            await connection.execute(
                text("""
                    INSERT INTO memory_proposals (
                        id, tenant_id, proxy_user_id, extraction_job_id, conversation_scope_id,
                        assistant_turn_id, assistant_content_sha256, status, expires_at
                    ) VALUES (
                        :id, :tenant_id, :proxy_user_id, :extraction_job_id, 'external:chat-26b',
                        'external:turn-1', :content_sha256, 'active', NOW() + INTERVAL '1 hour'
                    )
                """),
                {
                    "id": uuid.uuid4(),
                    "tenant_id": tenant_id,
                    "proxy_user_id": proxy_user_id,
                    "extraction_job_id": extraction_job_id,
                    "content_sha256": hashlib.sha256(b"proposal").hexdigest(),
                },
            )

        with pytest.raises(IntegrityError):
            async with engine.begin() as connection:
                await connection.execute(
                    text("""
                        INSERT INTO conversation_evidence_turns (
                            id, tenant_id, proxy_user_id, extraction_job_id, conversation_scope_id,
                            turn_id, role, content_sha256
                        ) VALUES (
                            :id, :tenant_id, :proxy_user_id, :extraction_job_id, 'external:chat-26b',
                            'external:turn-1', 'assistant', :content_sha256
                        )
                    """),
                    {
                        "id": uuid.uuid4(),
                        "tenant_id": tenant_id,
                        "proxy_user_id": proxy_user_id,
                        "extraction_job_id": extraction_job_id,
                        "content_sha256": hashlib.sha256(b"different").hexdigest(),
                    },
                )

        with pytest.raises(IntegrityError):
            async with engine.begin() as connection:
                await connection.execute(
                    text("""
                        INSERT INTO memory_proposals (
                            id, tenant_id, proxy_user_id, extraction_job_id, conversation_scope_id,
                            assistant_turn_id, assistant_content_sha256, status, expires_at
                        ) VALUES (
                            :id, :tenant_id, :proxy_user_id, :extraction_job_id, 'external:chat-26b',
                            'external:turn-1', :content_sha256, 'active', NOW() + INTERVAL '1 hour'
                        )
                    """),
                    {
                        "id": uuid.uuid4(),
                        "tenant_id": tenant_id,
                        "proxy_user_id": proxy_user_id,
                        "extraction_job_id": extraction_job_id,
                        "content_sha256": hashlib.sha256(b"different").hexdigest(),
                    },
                )
        # Exercise the actual service and a fresh session, not just raw constraints.
        job = {
            "job_id": str(uuid.uuid4()),
            "tenant_id": str(tenant_id),
            "proxy_user_id": str(proxy_user_id),
            "external_user_id": "phase26b-user",
            "external_conversation_id": "chat-26b",
            "messages": [{
                "turn_id": "external:turn-1",
                "role": "assistant",
                "source_kind": "assistant_output",
                "content": "proposal",
                "turn_content_sha256": hashlib.sha256(b"proposal").hexdigest(),
                "is_memory_proposal": True,
            }],
        }
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        async with sessions() as session:
            service = MemoryService(
                session=session, cache_service=MagicMock(),
                qdrant_service=MagicMock(), quota_manager=MagicMock(),
                embedding_service=MagicMock(),
            )
            result, created = await service._create_extraction_job(job)
            assert created and len(result["proposal_ids"]) == 1
        async with sessions() as session:
            payload = (await session.execute(
                text("SELECT payload FROM extraction_jobs WHERE id = :id"),
                {"id": uuid.UUID(job["job_id"])},
            )).scalar_one()
            assert payload["proposal_ids"] == result["proposal_ids"]
            service = MemoryService(
                session=session, cache_service=MagicMock(),
                qdrant_service=MagicMock(), quota_manager=MagicMock(),
                embedding_service=MagicMock(),
            )
            changed = {**job, "job_id": str(uuid.uuid4()), "messages": [{
                **job["messages"][0],
                "turn_content_sha256": hashlib.sha256(b"forged").hexdigest(),
            }]}
            with pytest.raises(APIError) as rejected:
                await service._create_extraction_job(changed)
            assert rejected.value.status_code == 409
            await session.rollback()
            other_chat = {**job, "job_id": str(uuid.uuid4()), "external_conversation_id": "other-chat"}
            other_result, _ = await service._create_extraction_job(other_chat)
            assert other_result["proposal_ids"] != result["proposal_ids"]
            other_user = uuid.uuid4()
            await session.execute(text(
                "INSERT INTO proxy_users (id, tenant_id, external_user_id, external_user_id_hash) "
                "VALUES (:id, :tenant, 'phase26b-other', :hash)"
            ), {"id": other_user, "tenant": tenant_id,
                "hash": hashlib.sha256(b"phase26b-other").hexdigest()})
            await session.commit()
            other_job = {**job, "job_id": str(uuid.uuid4()), "proxy_user_id": str(other_user),
                         "external_user_id": "phase26b-other"}
            other_result, _ = await service._create_extraction_job(other_job)
            assert other_result["proposal_ids"] != result["proposal_ids"]
            ordinary = {**job, "job_id": str(uuid.uuid4()), "messages": [{
                **job["messages"][0], "turn_id": "ordinary-assistant",
                "is_memory_proposal": False,
            }]}
            ordinary.pop("proposal_ids", None)
            ordinary_result, _ = await service._create_extraction_job(ordinary)
            assert not ordinary_result.get("proposal_ids")
    finally:
        async with engine.begin() as connection:
            await connection.execute(text("DELETE FROM tenants WHERE id = :tenant_id"), {"tenant_id": tenant_id})
        await engine.dispose()
