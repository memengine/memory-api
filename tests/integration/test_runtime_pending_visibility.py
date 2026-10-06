"""Immediate reads on accepted normal jobs; no paid providers or customer identities."""
import asyncio
import os
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from api.db.models import ExtractionJob, ExtractionJobStatus, Memory, ProxyUser
from api.schemas.preferences import PreferenceReadContext
from api.services.embedding_service import DEFAULT_ACTIVE_MODEL_ID, EmbeddingService
from api.services.preference_intent_admission_service import (
    PreferenceAdmissionUnavailable,
)
from api.services.retriever import RetrieverService
from api.services.runtime_preference_routing import RUNTIME_ROUTE
from api.settings import get_settings
from api.tasks import provenance_tasks
from tests.integration.test_preference_fact_transaction import fact
from tests.integration.test_preference_intent_admission import args, job
from tests.integration.test_runtime_preference_ingestion import ingestion as _ingestion
from tests.integration.test_runtime_preference_worker import (
    Cache,
    enable,
    seed,
    worker,
)
from tests.integration.test_runtime_preference_worker import (
    forbid_real_providers as _forbid_real_providers,
)
from tests.integration.test_runtime_preference_worker import (
    sql_scope as _sql_scope,
)
from tests.integration.test_source_decision_postgres import _decision
from tests.unit.test_preference_intent_contract import intent, reply

ingestion = _ingestion
sql_scope = _sql_scope
forbid_real_providers = _forbid_real_providers
_detached_async_embed = EmbeddingService.embed


def read(scope, ids=(), *, context=None, after_candidates=lambda: None, after_guard=lambda: None,
         owner=None, tenant=None, dirty=False):
    async def exercise():
        engine = create_async_engine(os.environ["DATABASE_URL"], pool_size=1, max_overflow=0, pool_timeout=2)
        try:
            async with async_sessionmaker(engine, expire_on_commit=False)() as session:
                service = RetrieverService(session=session, qdrant_service=SimpleNamespace(),
                    cache_service=AsyncMock(), quota_manager=AsyncMock(), proxy_user_service=SimpleNamespace(),
                    embedding_service=SimpleNamespace())
                service._queue_access_update = lambda _: None
                # A stale cache/vector page nominates IDs; all final guard,
                # ownership, content and preference eligibility queries are real.
                async def candidates(**kwargs):
                    service._resolved_read_proxy_id = str(owner or scope.proxy_id)
                    service.last_quota_mode = "FULL"
                    service.last_cache_hit = True
                    rows = list((await session.scalars(select(Memory).where(Memory.id.in_(ids)))).all())
                    result = [service._memory_to_result(m, semantic_score=1) for m in rows]
                    if dirty:
                        rows[0].content = "must not be committed by a read"
                    await asyncio.to_thread(after_candidates)
                    return result
                service._retrieve_candidates = candidates
                original_hydrate = service._hydrate_current_results
                async def hydrate(*args, **kwargs):
                    await asyncio.to_thread(after_guard)
                    return await original_hydrate(*args, **kwargs)
                service._hydrate_current_results = hydrate
                result = await service.retrieve("current defaults and exam?", external_user_id=scope.external_id,
                    tenant_id=str(tenant or scope.tenant_id), proxy_user_id=str(owner or scope.proxy_id),
                    preference_context=context)
                return result, service.last_memory_visibility, service.last_preference_decisions
        finally:
            await engine.dispose()
    return asyncio.run(exercise())


def preference_context():
    return PreferenceReadContext(property_keys=["examples.language"], applicability={"kind": "global"})


@pytest.mark.parametrize("explicit", [False, True])
def test_acceptance_blocks_immediate_stale_reads_before_capture(sql_scope, ingestion, monkeypatch, explicit):
    old = seed(sql_scope, monkeypatch)
    old_fact = fact(sql_scope)
    enable(sql_scope, monkeypatch)
    accepted = ingestion.send()
    with sql_scope.factory() as session:
        row = session.get(ExtractionJob, uuid.UUID(accepted["job_id"]))
        assert row.runtime_route == RUNTIME_ROUTE and row.runtime_applied_at is None
        assert row.preference_intent_admission is None
    items, visibility, decisions = read(sql_scope, [old, old_fact], context=preference_context() if explicit else None)
    assert items == decisions == [] and visibility.state == "processing"


@pytest.mark.parametrize("status", [ExtractionJobStatus.failed, ExtractionJobStatus.dead])
def test_failure_or_retention_never_times_out_into_old_current(sql_scope, ingestion, monkeypatch, status):
    old = fact(sql_scope)
    enable(sql_scope, monkeypatch)
    job_id = uuid.UUID(ingestion.send()["job_id"])
    with sql_scope.factory.begin() as session:
        row = session.get(ExtractionJob, job_id)
        row.status = status
        row.payload = {}  # Retention/dead-letter loss is not an applied proof.
    items, visibility, _ = read(sql_scope, [old])
    assert items == [] and visibility.state == "unavailable"


def test_rollback_before_capture_preserves_guard_and_forbids_legacy(sql_scope, ingestion, monkeypatch):
    old = fact(sql_scope)
    enable(sql_scope, monkeypatch)
    job_id = uuid.UUID(ingestion.send()["job_id"])
    monkeypatch.setattr(get_settings(), "runtime_preference_enabled", False)
    instance, model, embedder = worker(sql_scope)
    with pytest.raises(PreferenceAdmissionUnavailable, match="intent_rollout_unavailable"):
        instance.process(job_id)
    items, visibility, _ = read(sql_scope, [old])
    assert items == [] and visibility.state == "processing"
    assert not model.calls and not embedder.calls


def test_sql_receipt_releases_guard_even_when_cache_delivery_fails(sql_scope, ingestion, monkeypatch):
    old = seed(sql_scope, monkeypatch)
    enable(sql_scope, monkeypatch)
    job_id = uuid.UUID(ingestion.send(messages=[{"role": "user", "content": intent()["quote"]}])["job_id"])
    instance, model, _ = worker(sql_scope, [reply()], cache=Cache(failures=1))
    with pytest.raises(ConnectionError, match="cache outage"):
        instance.process(job_id)
    with sql_scope.factory() as session:
        row = session.get(ExtractionJob, job_id)
        assert row.status != ExtractionJobStatus.completed
        assert row.runtime_applied_at is not None and row.preference_intent_admission["state"] == "applied"
        stamp = row.runtime_applied_at
    # Old cached preference is archived; SQL ledger selects the new value even
    # though vectors/cache delivery and public job completion still lag.
    items, visibility, decisions = read(sql_scope, [old], context=preference_context())
    assert visibility is None and not decisions and len(items) == 1 and "Python" in items[0].content
    assert instance.process(job_id)["memories_created"] == 1 and len(model.calls) == 1
    with sql_scope.factory() as session:
        assert session.get(ExtractionJob, job_id).runtime_applied_at == stamp


def test_apply_commit_between_candidates_and_guard_never_returns_old_fact(sql_scope, ingestion, monkeypatch):
    old = fact(sql_scope)
    enable(sql_scope, monkeypatch)
    text = "my exam moved to October 18"
    job_id = uuid.UUID(ingestion.send(messages=[{"role": "user", "content": text}])["job_id"])
    instance, _, _ = worker(sql_scope, [reply(preferences=[], nonpreferences=[
        {"category": "fact", "content": text, "turn_index": 0, "quote": text}]), _decision(old, relation="supersedes")])
    items, visibility, _ = read(sql_scope, [old], after_candidates=lambda: instance.process(job_id))
    assert items == [] and visibility is None  # Empty is safe while replacement vector delivery lags.


def test_new_pending_acceptance_after_guard_is_outside_same_read_snapshot(sql_scope, ingestion, monkeypatch):
    old = fact(sql_scope)
    enable(sql_scope, monkeypatch)
    first = uuid.UUID(ingestion.send(messages=[{"role": "user", "content": "Hello"}])["job_id"])
    instance, _, _ = worker(sql_scope, [reply(preferences=[])])
    instance.process(first)
    items, visibility, _ = read(sql_scope, [old], after_guard=lambda: ingestion.send())
    assert len(items) == 1 and visibility is None  # Linearized at the short SQL snapshot.
    items, visibility, _ = read(sql_scope, [old])
    assert items == [] and visibility.state == "processing"


def test_foreign_tenant_and_blocked_owner_cannot_read(sql_scope, ingestion, monkeypatch):
    old = fact(sql_scope)
    enable(sql_scope, monkeypatch)
    ingestion.send()
    assert read(sql_scope, [old], tenant=uuid.uuid4())[0] == []
    with sql_scope.factory.begin() as session:
        session.get(ProxyUser, sql_scope.proxy_id).is_blocked = True
    assert read(sql_scope, [old])[0] == []


def test_read_cannot_commit_caller_mutation(sql_scope, ingestion, monkeypatch):
    old = fact(sql_scope)
    enable(sql_scope, monkeypatch)
    ingestion.send()
    with pytest.raises(ValueError, match="read-only session"):
        read(sql_scope, [old], dirty=True)
    with sql_scope.factory() as session:
        assert session.get(Memory, old).content == "my exam is on October 10"


def test_legacy_authority_is_not_changed_by_pending_guard(sql_scope, ingestion, monkeypatch):
    old = fact(sql_scope)
    with sql_scope.factory.begin() as session:
        row = session.get(Memory, old)
        row.metadata_json = {"provenance": {"attestation": "legacy_conversation", "authority_priority": 50}}
    enable(sql_scope, monkeypatch)
    ingestion.send()
    assert read(sql_scope, [old])[1].state == "processing"
    with sql_scope.factory() as session:
        assert session.get(Memory, old).metadata_json["provenance"]["authority_priority"] == 50


def test_pre_reservation_capture_and_retention_keep_guard(sql_scope, monkeypatch):
    old = fact(sql_scope)
    enable(sql_scope, monkeypatch)
    job_id = job(sql_scope)
    instance, _, _ = worker(sql_scope)
    instance.transactions.capture(**args(sql_scope, job_id))
    with sql_scope.factory.begin() as session:
        row = session.get(ExtractionJob, job_id)
        row.runtime_route = None  # A private capture from the previous schema.
        row.raw_payload_expires_at = datetime.now(UTC) - timedelta(seconds=1)
    assert read(sql_scope, [old])[1].state == "processing"
    monkeypatch.setattr(provenance_tasks, "build_sync_session_factory", lambda: sql_scope.factory)
    assert provenance_tasks.redact_expired_extraction_payloads.run()["redacted_jobs"] == 1
    items, visibility, _ = read(sql_scope, [old])
    assert items == [] and visibility.state == "unavailable"
    with sql_scope.factory() as session:
        row = session.get(ExtractionJob, job_id)
        assert row.runtime_route == RUNTIME_ROUTE and row.runtime_applied_at is None
        assert row.preference_intent_admission is None


def test_pre_reservation_applied_receipt_requires_full_validation_before_release(sql_scope, monkeypatch):
    old = fact(sql_scope)
    enable(sql_scope, monkeypatch)
    instance, _, _ = worker(sql_scope, [reply(preferences=[])])
    job_id = job(sql_scope)
    instance.process(job_id)
    with sql_scope.factory.begin() as session:
        row = session.get(ExtractionJob, job_id)
        row.runtime_route = row.runtime_applied_at = None
    assert read(sql_scope, [old])[1].state == "processing"
    # Valid old receipt delivery can resume after rollout rollback.
    monkeypatch.setattr(get_settings(), "runtime_preference_enabled", False)
    instance.process(job_id)
    items, visibility, _ = read(sql_scope, [old])
    assert len(items) == 1 and visibility is None


def test_large_backlog_is_an_indexed_boolean_not_job_payload_loading(sql_scope, monkeypatch):
    old = fact(sql_scope)
    enable(sql_scope, monkeypatch)
    monkeypatch.setattr(get_settings(), "runtime_target_release_enabled", True)
    now = datetime.now(UTC)
    with sql_scope.factory.begin() as session:
        session.add_all([ExtractionJob(id=uuid.uuid4(), tenant_id=sql_scope.tenant_id,
            proxy_user_id=sql_scope.proxy_id, external_user_id=sql_scope.external_id,
            runtime_route=RUNTIME_ROUTE, runtime_applied_at=now if n < 2900 else None,
            runtime_visibility_finalized_at=now if n < 2900 else None,
            status=ExtractionJobStatus.completed if n < 2900 else ExtractionJobStatus.queued,
            payload={}) for n in range(3000)])
    assert read(sql_scope, [old])[1].state == "processing"
    with sql_scope.factory() as session:
        # Check the exact final-query predicate can use the partial index even
        # in a prepared generic plan. This is not a production p99/load claim.
        session.execute(text("ANALYZE extraction_jobs"))
        session.execute(text("SET LOCAL enable_seqscan = off"))
        from api.services.runtime_fact_visibility import unresolved_runtime_predicate
        query = select(ExtractionJob.id).where(RetrieverService._runtime_job_predicate(),
            ExtractionJob.runtime_hold_released_at.is_(None),
            unresolved_runtime_predicate(), ExtractionJob.tenant_id == sql_scope.tenant_id,
            ExtractionJob.proxy_user_id == sql_scope.proxy_id)
        plan = session.execute(text("EXPLAIN " + str(query.compile(compile_kwargs={"literal_binds": True})))).scalars().all()
        assert "ix_extraction_jobs_runtime_visibility" in " ".join(plan), plan


@pytest.mark.parametrize("quota_mode", ["FULL", "BLOCKED", "PASSTHROUGH"])
def test_pending_preflight_skips_cache_embedding_and_vector_work(sql_scope, ingestion, monkeypatch, quota_mode):
    enable(sql_scope, monkeypatch)
    ingestion.send()
    async def exercise():
        engine = create_async_engine(os.environ["DATABASE_URL"])
        try:
            async with async_sessionmaker(engine, expire_on_commit=False)() as session:
                cache = AsyncMock()
                service = RetrieverService(session=session, qdrant_service=SimpleNamespace(),
                    cache_service=cache, quota_manager=SimpleNamespace(), proxy_user_service=SimpleNamespace(),
                    embedding_service=SimpleNamespace())
                service._embed_query = AsyncMock(side_effect=AssertionError("embedding on pending read"))
                service._search_qdrant = AsyncMock(side_effect=AssertionError("vector on pending read"))
                assert await service.retrieve("current?", external_user_id=sql_scope.external_id,
                    tenant_id=str(sql_scope.tenant_id), proxy_user_id=str(sql_scope.proxy_id), quota_mode=quota_mode) == []
                assert service.last_memory_visibility.state == "processing"
                cache.get_retrieval_results.assert_not_awaited()
        finally:
            await engine.dispose()
    asyncio.run(exercise())


def test_query_embedding_releases_sql_before_provider_on_one_connection_pool(sql_scope, monkeypatch):
    async def exercise():
        engine = create_async_engine(os.environ["DATABASE_URL"], pool_size=1, max_overflow=0, pool_timeout=2)
        embedder = None
        try:
            async with async_sessionmaker(engine, expire_on_commit=False)() as session:
                embedder = EmbeddingService(async_session=session)
                # Restore only the bounded async wrapper, with its actual
                # provider implementation replaced below by a controlled fake.
                monkeypatch.setattr(embedder, "embed", _detached_async_embed.__get__(embedder, EmbeddingService))
                seen = []
                def provider(text, model, tenant):
                    assert engine.sync_engine.pool.checkedout() == 0
                    seen.append((text, model.id))
                    return [0.1] * model.dimensions
                monkeypatch.setattr(embedder, "_embed_with_model_sync", provider)
                service = RetrieverService(session=session, qdrant_service=SimpleNamespace(),
                    cache_service=AsyncMock(), quota_manager=SimpleNamespace(), embedding_service=embedder)
                result = await service._embed_query("synthetic", model_id=DEFAULT_ACTIVE_MODEL_ID)
                assert result.model_id == DEFAULT_ACTIVE_MODEL_ID and seen == [("synthetic", DEFAULT_ACTIVE_MODEL_ID)]
        finally:
            if embedder is not None:
                embedder.sync_http_client.close()
                embedder.sync_redis_client.close()
                await embedder.async_http_client.aclose()
                await embedder.async_redis_client.aclose()
            await engine.dispose()
    asyncio.run(exercise())
