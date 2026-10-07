"""Real authenticated HTTP/SQL/Redis ordering; semantic providers are forbidden."""
import asyncio
import os
import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from api import dependencies
from api.db.cache import CacheService
from api.db.models import ApiKey, ExtractionJob, ExtractionJobStatus, MemorySourceEvent, QuotaMode, Tenant
from api.dependencies import DbSession
from api.main import _register_exception_handlers
from api.middleware.auth import AuthMiddleware
from api.middleware.rate_limiter import RateLimiterMiddleware
from api.routers import memories
from api.services.context_builder import ContextBuilder
from api.services.memory_service import MemoryService
from api.services.proxy_user_service import ProxyUserService
from api.services.quality_gate import QualityGateService
from api.services.retriever import RetrieverService
from api.settings import get_settings
from api.utils.crypto import api_key_prefix, hash_api_key
from tests.integration.test_runtime_preference_worker import enable, seed
from tests.integration.test_preference_snapshots import forbid_real_providers as _forbid
from tests.integration.test_source_decision_postgres import sql_scope as _scope

sql_scope = _scope
forbid_real_providers = _forbid


@pytest.fixture
def turn_http(sql_scope, monkeypatch):
    seed(sql_scope, monkeypatch)
    enable(sql_scope, monkeypatch)
    monkeypatch.setattr(get_settings(), "runtime_turn_preparation_enabled", True)
    raw_key = f"turn-test-{uuid.uuid4()}"
    key_id = uuid.uuid4()
    with sql_scope.factory.begin() as session:
        session.add(ApiKey(id=key_id, tenant_id=sql_scope.tenant_id, key_hash=hash_api_key(raw_key),
            key_prefix=api_key_prefix(raw_key), name="isolated turn test", permissions=["read", "write"]))

    @asynccontextmanager
    async def serve(*, queue_full=False, broker_failed=False, read_failed=False, before_commit=None, embeddings=None):
        engine = create_async_engine(os.environ["DATABASE_URL"])
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        cache = CacheService(redis_url=os.environ["REDIS_URL"], use_direct_breaker=True)
        quota = SimpleNamespace(get_quota_envelope=AsyncMock(return_value=SimpleNamespace(mode=QuotaMode.full)))
        dispatched = []
        def dispatch(name, **kwargs):
            dispatched.append(kwargs["args"][0])
            if broker_failed:
                raise ConnectionError("controlled broker failure")
        async def db():
            async with sessions() as session:
                yield session
        async def proxy(session: DbSession):
            return ProxyUserService(session=session, cache_service=cache)
        async def service(session: DbSession):
            instance = MemoryService(session=session, cache_service=cache, qdrant_service=SimpleNamespace(),
                quota_manager=quota, dispatch_task=dispatch, embedding_service=embeddings or SimpleNamespace())
            if queue_full:
                instance.queue_router.reserve_extraction_slot = AsyncMock(return_value=None)
            if before_commit is not None:
                original = instance._persist_evidence_ledger
                async def persist(**kwargs):
                    await original(**kwargs)
                    await before_commit()
                instance._persist_evidence_ledger = persist
            return instance
        async def gate(session: DbSession):
            return QualityGateService(session=session, cache_service=cache, embedding_service=SimpleNamespace())
        async def retrieval(session: DbSession):
            instance = RetrieverService(session=session, cache_service=cache, qdrant_service=SimpleNamespace(),
                quota_manager=quota, proxy_user_service=await proxy(session), embedding_service=SimpleNamespace())
            if read_failed:
                instance.retrieve = AsyncMock(side_effect=RuntimeError("controlled read failure"))
            return instance
        app = FastAPI()
        _register_exception_handlers(app)
        app.include_router(memories.router)
        app.state.cache_service = cache
        app.dependency_overrides.update({dependencies.get_db_session: db, dependencies.get_memory_service: service,
            dependencies.get_proxy_user_service: proxy, dependencies.get_quality_gate_service: gate,
            dependencies.get_retriever_service: retrieval, dependencies.get_context_builder: ContextBuilder,
            dependencies.get_cache_service: lambda: cache})
        app.add_middleware(RateLimiterMiddleware)
        async with httpx.AsyncClient() as auth_http:
            app.add_middleware(AuthMiddleware, session_factory=sessions, redis_client=cache.client, http_client=auth_http)
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test",
                headers={"Authorization": f"ApiKey {raw_key}"}) as client:
                try:
                    yield client, dispatched, sessions, cache
                finally:
                    await cache.client.aclose()
                    await engine.dispose()
    return serve


def body(scope, **changes):
    return {"external_user_id": scope.external_id, "query": "Which language should you use?",
        "message": "No", "conversation_id": "turn-check", "turn_id": "turn-1", **changes}


@pytest.mark.parametrize("text", ["No", "Python instead", "अभी तय नहीं है", "abhi decide nahi kiya"])
def test_current_turn_commits_guard_before_read_and_never_activates_old_default(sql_scope, turn_http, text):
    async def exercise():
        async with turn_http() as (client, dispatched, sessions, cache):
            cache.set_job_status = AsyncMock(side_effect=AssertionError("cache is not admission proof"))
            response = await client.post("/v1/memories/prepare-turn", json=body(sql_scope, message=text))
            assert response.status_code == 200, response.text
            data = response.json()
            assert data["admission"]["state"] == "accepted" and data["data"] == []
            assert data["memory_visibility"] == {"state": "processing", "scope": "user_learned_context"}
            assert data["clarification_question"] is None
            async with sessions() as session:
                row = await session.get(ExtractionJob, uuid.UUID(data["admission"]["job_id"]))
                assert row.runtime_route == "preference_v1" and row.runtime_applied_at is None
                assert row.payload["messages"][-1]["content"] == text
                assert row.payload["messages"][-1]["source_kind"] == "client_assertion"
                event = await session.get(MemorySourceEvent, row.source_event_id)
                assert event.processing_metadata["turn_preparation_sha256"]
            assert len(dispatched) == 1 and "messages" not in dispatched[0]
            cache.set_job_status.assert_not_awaited()
    asyncio.run(exercise())


def test_exact_retry_reuses_admission_but_changed_payload_conflicts(sql_scope, turn_http):
    async def exercise():
        async with turn_http() as (client, dispatched, sessions, _):
            original = await client.post("/v1/memories/prepare-turn", json=body(sql_scope))
            repeated = await client.post("/v1/memories/prepare-turn", json=body(sql_scope, query="different read query"))
            changed = await client.post("/v1/memories/prepare-turn", json=body(sql_scope, message="Python"))
            assert original.status_code == repeated.status_code == 200
            assert repeated.json()["admission"] == {**original.json()["admission"], "state": "replayed"}
            assert changed.status_code == 409 and len(dispatched) == 1
            async with sessions() as session:
                assert await session.scalar(select(func.count()).select_from(ExtractionJob).where(
                    ExtractionJob.proxy_user_id == sql_scope.proxy_id,
                    ExtractionJob.payload["turn_preparation_sha256"].astext.is_not(None))) == 1
    asyncio.run(exercise())


@pytest.mark.parametrize("case", ["queue_full", "broker_failed", "read_failed"])
def test_failure_cannot_become_ordinary_stale_read_success(sql_scope, turn_http, case):
    async def exercise():
        async with turn_http(**{case: True}) as (client, _, sessions, _):
            response = await client.post("/v1/memories/prepare-turn", json=body(sql_scope))
            assert response.status_code == 200, response.text
            data = response.json()
            assert data["data"] == [] and data["clarification_question"] is None
            if case == "queue_full":
                assert data["admission"]["state"] == "rejected" and data["admission"]["job_id"] is None
            else:
                assert data["admission"]["state"] == "accepted"
                async with sessions() as session:
                    row = await session.get(ExtractionJob, uuid.UUID(data["admission"]["job_id"]))
                    assert row.status == ExtractionJobStatus.queued and row.runtime_route == "preference_v1"
            assert data["memory_visibility"]["state"] == ("processing" if case == "broker_failed" else "unavailable")
    asyncio.run(exercise())


@pytest.mark.parametrize("permissions", [["read"], ["write"]])
def test_combined_operation_requires_both_permissions(sql_scope, turn_http, permissions):
    with sql_scope.factory.begin() as session:
        key = session.scalars(select(ApiKey).where(ApiKey.tenant_id == sql_scope.tenant_id)).one()
        key.permissions = permissions
    async def exercise():
        async with turn_http() as (client, dispatched, _, _):
            response = await client.post("/v1/memories/prepare-turn", json=body(sql_scope))
            assert response.status_code == 403 and not dispatched
    asyncio.run(exercise())


def test_same_client_ids_are_separate_owned_admissions(sql_scope, turn_http):
    async def exercise():
        async with turn_http() as (client, _, sessions, _):
            own = await client.post("/v1/memories/prepare-turn", json=body(sql_scope))
            other = await client.post("/v1/memories/prepare-turn", json=body(sql_scope, external_user_id="different-owner"))
            assert own.status_code == other.status_code == 200
            assert own.json()["admission"]["job_id"] != other.json()["admission"]["job_id"]
            async with sessions() as session:
                row = await session.get(ExtractionJob, uuid.UUID(other.json()["admission"]["job_id"]))
                assert row.proxy_user_id != sql_scope.proxy_id
                sql_scope.proxy_ids.append(row.proxy_user_id)
    asyncio.run(exercise())


def test_default_off_has_no_admission_side_effect(sql_scope, turn_http, monkeypatch):
    monkeypatch.setattr(get_settings(), "runtime_turn_preparation_enabled", False)
    async def exercise():
        async with turn_http() as (client, dispatched, sessions, _):
            response = await client.post("/v1/memories/prepare-turn", json=body(sql_scope))
            assert response.status_code == 409 and not dispatched
            async with sessions() as session:
                assert await session.scalar(select(func.count()).select_from(ExtractionJob).where(
                    ExtractionJob.proxy_user_id == sql_scope.proxy_id,
                    ExtractionJob.payload["turn_preparation_sha256"].astext.is_not(None))) == 0
    asyncio.run(exercise())


def test_concurrent_retry_fails_fast_then_replays_one_committed_job(sql_scope, turn_http):
    async def exercise():
        entered, release = asyncio.Event(), asyncio.Event()
        async def before_commit():
            entered.set()
            await asyncio.wait_for(release.wait(), timeout=5)
        async with turn_http(before_commit=before_commit) as (client, dispatched, sessions, _):
            first = asyncio.create_task(client.post("/v1/memories/prepare-turn", json=body(sql_scope)))
            try:
                await asyncio.wait_for(entered.wait(), timeout=5)
                contender = await asyncio.wait_for(client.post("/v1/memories/prepare-turn", json=body(sql_scope)), timeout=3)
                assert contender.status_code == 409
                assert contender.json()["error"] == "turn_admission_in_progress"
            finally:
                release.set()
                accepted = await first
            replay = await client.post("/v1/memories/prepare-turn", json=body(sql_scope))
            assert accepted.status_code == replay.status_code == 200
            assert replay.json()["admission"] == {**accepted.json()["admission"], "state": "replayed"}
            assert len(dispatched) == 1
            async with sessions() as session:
                assert await session.scalar(select(func.count()).select_from(ExtractionJob).where(
                    ExtractionJob.proxy_user_id == sql_scope.proxy_id,
                    ExtractionJob.payload["turn_preparation_sha256"].astext.is_not(None))) == 1
    asyncio.run(exercise())


def test_redacted_completed_job_remains_bound_and_unapplied_context_stays_withheld(sql_scope, turn_http):
    async def exercise():
        async with turn_http() as (client, dispatched, sessions, _):
            original = await client.post("/v1/memories/prepare-turn", json=body(sql_scope))
            job_id = uuid.UUID(original.json()["admission"]["job_id"])
            async with sessions() as session:
                row = await session.get(ExtractionJob, job_id)
                row.payload = {}
                row.payload_envelope = None
                row.payload_redacted_at = datetime.now(UTC)
                row.status = ExtractionJobStatus.completed
                row.runtime_applied_at = None
                await session.commit()
            replay = await client.post("/v1/memories/prepare-turn", json=body(sql_scope))
            changed = await client.post("/v1/memories/prepare-turn", json=body(sql_scope, message="Python"))
            assert replay.status_code == 200 and replay.json()["admission"]["state"] == "replayed"
            assert replay.json()["admission"]["job_id"] == str(job_id)
            assert replay.json()["data"] == [] and replay.json()["memory_visibility"] is not None
            assert changed.status_code == 409 and len(dispatched) == 1
    asyncio.run(exercise())


def test_transaction_failure_cannot_acknowledge_or_read_stale_context(sql_scope, turn_http, monkeypatch):
    read = AsyncMock(side_effect=AssertionError("uncommitted admission must never read"))
    monkeypatch.setattr(memories, "retrieve_memories", read)
    async def fail():
        raise RuntimeError("controlled precommit failure")
    async def exercise():
        async with turn_http(before_commit=fail) as (client, dispatched, sessions, _):
            with pytest.raises(RuntimeError, match="controlled precommit failure"):
                await client.post("/v1/memories/prepare-turn", json=body(sql_scope))
            assert not dispatched
            read.assert_not_awaited()
            async with sessions() as session:
                assert await session.scalar(select(func.count()).select_from(ExtractionJob).where(
                    ExtractionJob.proxy_user_id == sql_scope.proxy_id,
                    ExtractionJob.payload["turn_preparation_sha256"].astext.is_not(None))) == 0
                assert await session.scalar(select(func.count()).select_from(MemorySourceEvent).where(
                    MemorySourceEvent.proxy_user_id == sql_scope.proxy_id,
                    MemorySourceEvent.processing_metadata["turn_preparation_sha256"].astext.is_not(None))) == 0
    asyncio.run(exercise())


def test_tenant_identity_is_part_of_exact_turn_binding(sql_scope, turn_http, monkeypatch):
    tenant_id = uuid.uuid4()
    raw_key = f"turn-test-{uuid.uuid4()}"
    with sql_scope.factory.begin() as session:
        original = session.get(Tenant, sql_scope.tenant_id)
        session.add(Tenant(id=tenant_id, company_name="Other isolated turn tenant", region_id="IN1",
            plan_tier=original.plan_tier, metadata_json=original.metadata_json))
        session.add(ApiKey(id=uuid.uuid4(), tenant_id=tenant_id, key_hash=hash_api_key(raw_key),
            key_prefix=api_key_prefix(raw_key), name="isolated other tenant", permissions=["read", "write"]))
    sql_scope.tenant_ids.append(tenant_id)
    monkeypatch.setattr(get_settings(), "runtime_preference_tenant_ids", f"{sql_scope.tenant_id},{tenant_id}")
    async def exercise():
        async with turn_http() as (client, _, sessions, _):
            first = await client.post("/v1/memories/prepare-turn", json=body(sql_scope))
            second = await client.post("/v1/memories/prepare-turn", json=body(sql_scope),
                headers={"Authorization": f"ApiKey {raw_key}"})
            assert first.status_code == second.status_code == 200, second.text
            assert first.json()["admission"]["job_id"] != second.json()["admission"]["job_id"]
            async with sessions() as session:
                row = await session.get(ExtractionJob, uuid.UUID(second.json()["admission"]["job_id"]))
                assert row.tenant_id == tenant_id and row.proxy_user_id != sql_scope.proxy_id
                sql_scope.proxy_ids.append(row.proxy_user_id)
    asyncio.run(exercise())


def test_prepared_turn_reaches_existing_worker_without_authority_upgrade(sql_scope, turn_http):
    from tests.integration.test_runtime_preference_worker import worker
    from tests.integration.test_preference_choice_operations import read
    from tests.integration.test_preference_snapshots import identity
    from tests.unit.test_preference_intent_contract import intent, reply
    instance, model, embedder = worker(sql_scope, [reply()])
    async def exercise():
        async with turn_http() as (client, _, sessions, _):
            admitted = await client.post("/v1/memories/prepare-turn", json=body(sql_scope, message=intent()["quote"]))
            job_id = uuid.UUID(admitted.json()["admission"]["job_id"])
            receipt = await asyncio.to_thread(instance.process, job_id)
            assert receipt["memories_created"] == 1
            assert identity(read(sql_scope)).target.value == "Python"
            async with sessions() as session:
                row = await session.get(ExtractionJob, job_id)
                assert row.runtime_applied_at is not None
                memory = receipt["stored_memories"][0]
                from api.db.models import Memory
                stored = await session.get(Memory, uuid.UUID(memory["id"]))
                provenance = stored.metadata_json["provenance"]
                assert provenance["authority_rules"]["default_priority"] == 20
                assert provenance["authority_priority"] == 20 and provenance["attestation"] == "client_asserted"
            assert len(model.calls) == len(embedder.calls) == 1
    asyncio.run(exercise())
