"""Queue -> fixed intent -> receipt -> governed read, controlled providers only."""
import json
import uuid
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import sessionmaker

from api.db.models import ExtractionJob, ExtractionJobStatus, Memory, ProxyUser, Tenant
from api.services.preference_intent_admission_service import (
    PreferenceAdmissionUnavailable,
)
from api.services.runtime_preference_worker import (
    CATALOG_KEY,
    RuntimePreferenceTransactionService,
    RuntimePreferenceWorker,
)
from api.settings import get_settings
from api.tasks import extraction_tasks
from tests.integration.test_preference_choice_operations import counts, read
from tests.integration.test_preference_choice_policy import read_decisions
from tests.integration.test_preference_fact_transaction import Model, fact
from tests.integration.test_preference_intent_admission import args, job
from tests.integration.test_preference_intent_transaction import FACT, TEXT
from tests.integration.test_preference_intent_transaction import (
    forbid_real_providers as _forbid,
)
from tests.integration.test_preference_snapshots import identity, offered, seed
from tests.integration.test_source_decision_postgres import _decision, _embedding
from tests.integration.test_source_decision_postgres import sql_scope as _sql_scope
from tests.unit.test_preference_intent_contract import intent, reply

sql_scope = _sql_scope
forbid_real_providers = _forbid


def stored(scope, job_id):
    with scope.factory() as session:
        return RuntimePreferenceTransactionService._read(session.get(ExtractionJob, job_id))


class Embeddings:
    def __init__(self, check=lambda: None):
        self.calls, self.check = [], check

    def get_active_model_sync(self, *, session):
        # Model configuration reads are allowed; provider requests are not.
        session.execute(select(Tenant.id).limit(1))
        return SimpleNamespace(id="controlled-model")

    def embed_sync(self, text, **_kwargs):
        self.check()
        self.calls.append(text)
        return _embedding(text)


class Cache:
    def __init__(self, failures=0, check=lambda: None):
        self.failures, self.check, self.calls = failures, check, []

    async def invalidate_user_cache(self, profile, *, strict):
        self.check()
        assert strict
        self.calls.append(profile)
        if self.failures:
            self.failures -= 1
            raise ConnectionError("controlled cache outage")


def enable(scope, monkeypatch, **metadata):
    monkeypatch.setattr(get_settings(), "runtime_preference_enabled", True)
    monkeypatch.setattr(get_settings(), "runtime_preference_tenant_ids", str(scope.tenant_id))
    catalog = offered(scope).model_dump(mode="json")
    catalog.pop("tenant_id")
    with scope.factory.begin() as session:
        tenant = session.get(Tenant, scope.tenant_id)
        tenant.metadata_json = {CATALOG_KEY: catalog, **metadata}


def worker(scope, responses=(), *, cache=None, check=lambda: None):
    model, embedder = Model(responses, check), Embeddings(check)
    instance = RuntimePreferenceWorker(scope.factory, llm_service=model, embedding_service=embedder,
                                       cache_service=cache or Cache(check=check))
    return instance, model, embedder


def test_default_off_ignores_public_catalog_and_broker_switches(sql_scope, monkeypatch):
    monkeypatch.setattr(get_settings(), "runtime_preference_enabled", False)
    job_id = job(sql_scope, runtime_preference_enabled=True, runtime_preference_catalog={"forged": True})
    instance, model, embedder = worker(sql_scope)
    assert instance.process(job_id) is None
    assert model.calls == embedder.calls == []
    with sql_scope.factory() as session:
        assert session.get(ExtractionJob, job_id).preference_intent_admission is None


@pytest.mark.parametrize("field,value", [
    ("runtime_preference_enabled", False), ("runtime_preference_tenant_ids", str(uuid.UUID(int=1))),
    ("explicit_preference_claims_enabled", False),
])
def test_uncaptured_rollout_requires_every_gate(sql_scope, monkeypatch, field, value):
    enable(sql_scope, monkeypatch)
    monkeypatch.setattr(get_settings(), field, value)
    instance, model, _ = worker(sql_scope)
    assert instance.process(job(sql_scope)) is None and not model.calls


@pytest.mark.parametrize("payload", [
    {"preference_claims": []}, {"source": {"explicit": True}}, {"agent_id": str(uuid.UUID(int=1))},
    {"source_event_id": str(uuid.UUID(int=2))},
])
def test_registered_explicit_agent_paths_remain_legacy(sql_scope, monkeypatch, payload):
    enable(sql_scope, monkeypatch)
    instance, model, _ = worker(sql_scope)
    assert instance.process(job(sql_scope, **payload)) is None and not model.calls


@pytest.mark.parametrize("catalog", [None, {}, {"tenant_id": str(uuid.UUID(int=1))},
                                     {"properties": [], "scopes": []}, "x" * 33000],
                         ids=["missing", "empty", "foreign-owner", "empty-arrays", "oversized"])
def test_selected_invalid_catalog_is_failure_not_legacy(sql_scope, monkeypatch, catalog):
    enable(sql_scope, monkeypatch, **{CATALOG_KEY: catalog})
    instance, model, _ = worker(sql_scope)
    with pytest.raises(PreferenceAdmissionUnavailable, match="intent_catalog"):
        instance.process(job(sql_scope))
    assert not model.calls


def test_mixed_worker_receipt_and_governed_retrieval(sql_scope, monkeypatch):
    old_preference, old_fact = seed(sql_scope, monkeypatch), fact(sql_scope)
    enable(sql_scope, monkeypatch)
    job_id = job(sql_scope, messages=[{"role": "user", "content": TEXT}], external_conversation_id="runtime-check")
    instance, model, embedder = worker(sql_scope, [reply(nonpreferences=[FACT]), _decision(old_fact, relation="supersedes")])
    result = instance.process(job_id)
    assert len(result["stored_memories"]) == result["memories_created"] == 2
    assert identity(read(sql_scope)).target.value == "Python"
    prefs, decisions = read_decisions(sql_scope, {"kind": "global"})
    assert len(prefs) == 1 and prefs[0].id in {m["id"] for m in result["stored_memories"]}
    assert "Python" in prefs[0].content and decisions == []
    with sql_scope.factory() as session:
        assert session.get(Memory, old_preference).is_archived and session.get(Memory, old_fact).is_archived
        rows = session.scalars(select(Memory).where(Memory.proxy_user_id == sql_scope.proxy_id,
                                                   Memory.is_archived.is_(False))).all()
        assert {r.content for r in rows if r.category.value == "fact"} == {FACT["content"]}
        assert all(r.metadata_json["provenance"]["attestation"] == "client_asserted" for r in rows)
        assert all(r.metadata_json["provenance"]["external_conversation_id"] == "runtime-check" for r in rows)
    before = counts(sql_scope)
    restarted, empty_model, empty_embeddings = worker(sql_scope)
    assert restarted.process(job_id) == result and counts(sql_scope) == before
    assert empty_model.calls == empty_embeddings.calls == []
    assert len(model.calls) == len(embedder.calls) == 2
    assert "messages" not in result and "snapshot" not in json.dumps(result)


def test_uncertainty_withholds_without_new_value_then_decided_choice(sql_scope, monkeypatch):
    seed(sql_scope, monkeypatch)
    enable(sql_scope, monkeypatch)
    uncertain = "I'm torn between the two; I don't know which default to keep."
    job_id = job(sql_scope, messages=[{"role": "user", "content": uncertain}])
    instance, _, embedder = worker(sql_scope, [reply(preferences=[intent(action="withhold", value=None, quote=uncertain)])])
    result = instance.process(job_id)
    assert result["memories_created"] == 0 and result["extraction_metadata"]["withheld"] == 1
    assert not embedder.calls
    items, decisions = read_decisions(sql_scope, {"kind": "global"})
    assert items == [] and len(decisions) == 1 and decisions[0].state == "current_choice_unresolved"
    instance, _, _ = worker(sql_scope, [reply()])
    instance.process(job(sql_scope, messages=[{"role": "user", "content": intent()["quote"]}]))
    items, decisions = read_decisions(sql_scope, {"kind": "global"})
    assert len(items) == 1 and "Python" in items[0].content and not decisions


def test_scoped_replacement_preserves_global(sql_scope, monkeypatch):
    seed(sql_scope, monkeypatch)
    seed(sql_scope, monkeypatch, scope={"kind": "context", "namespace": "project", "key": "Cedar"})
    enable(sql_scope, monkeypatch)
    text = "For Cedar only, Use Python instead of C++. Outside Cedar is unchanged."
    instance, _, _ = worker(sql_scope, [reply(preferences=[intent(scope_ref="cedar", quote=text)])])
    instance.process(job(sql_scope, messages=[{"role": "user", "content": text}]))
    snapshot = read(sql_scope)
    assert identity(snapshot).target.value == "C++" and identity(snapshot, "cedar").target.value == "Python"
    items, _ = read_decisions(sql_scope, {"kind": "context", "namespace": "project", "key": "Cedar"})
    assert len(items) == 1 and "Python" in items[0].content


@pytest.mark.parametrize("field,value", [
    ("runtime_preference_enabled", False), ("runtime_preference_tenant_ids", ""),
    ("explicit_preference_claims_enabled", False),
])
def test_capture_survives_flag_rollback_without_fallback(sql_scope, monkeypatch, field, value):
    enable(sql_scope, monkeypatch)
    instance, model, _ = worker(sql_scope)
    job_id = job(sql_scope)
    instance.transactions.capture(**args(sql_scope, job_id))
    monkeypatch.setattr(get_settings(), field, value)
    with pytest.raises(PreferenceAdmissionUnavailable):
        instance.process(job_id)
    assert stored(sql_scope, job_id).state == "captured" and not model.calls


def test_capture_uses_frozen_catalog_after_configuration_removal(sql_scope, monkeypatch):
    seed(sql_scope, monkeypatch)
    enable(sql_scope, monkeypatch)
    instance, _, _ = worker(sql_scope, [reply()])
    job_id = job(sql_scope, messages=[{"role": "user", "content": intent()["quote"]}])
    instance.transactions.capture(**args(sql_scope, job_id))
    with sql_scope.factory.begin() as session:
        session.get(Tenant, sql_scope.tenant_id).metadata_json = {}
    assert instance.process(job_id)["memories_created"] == 1
    assert identity(read(sql_scope)).target.value == "Python"


@pytest.mark.parametrize("bad_turn", [
    {"role": "assistant", "content": intent()["quote"]},
    {"role": "tool", "content": intent()["quote"]},
    {"role": "user", "content": intent()["quote"], "source_kind": "tool_result"},
])
def test_nonuser_evidence_cannot_write(sql_scope, monkeypatch, bad_turn):
    enable(sql_scope, monkeypatch)
    instance, _, embedder = worker(sql_scope, [reply()])
    before = counts(sql_scope)
    with pytest.raises(PreferenceAdmissionUnavailable, match="response_invalid"):
        instance.process(job(sql_scope, messages=[bad_turn]))
    assert not embedder.calls and counts(sql_scope) == before


def test_provider_failure_keeps_capture_no_writes(sql_scope, monkeypatch):
    enable(sql_scope, monkeypatch)
    instance, _, embedder = worker(sql_scope)
    job_id = job(sql_scope)
    before = counts(sql_scope)
    with pytest.raises(StopIteration):
        instance.process(job_id)
    assert stored(sql_scope, job_id).state == "captured"
    assert not embedder.calls and counts(sql_scope) == before


def test_crash_after_admission_restarts_without_reclassification(sql_scope, monkeypatch):
    seed(sql_scope, monkeypatch)
    enable(sql_scope, monkeypatch)
    instance, model, embedder = worker(sql_scope, [reply()])
    job_id = job(sql_scope, messages=[{"role": "user", "content": intent()["quote"]}])
    def fail(*_args, **_kwargs):
        raise ConnectionError("controlled embedding outage")
    monkeypatch.setattr(embedder, "embed_sync", fail)
    before = counts(sql_scope)
    with pytest.raises(ConnectionError):
        instance.process(job_id)
    record = stored(sql_scope, job_id)
    assert record.state == "validated" and counts(sql_scope) == before
    restarted, empty_model, _ = worker(sql_scope)
    assert restarted.process(job_id)["memories_created"] == 1
    assert stored(sql_scope, job_id).capture_id == record.capture_id
    assert len(model.calls) == 1 and empty_model.calls == []


def test_rollback_flag_during_provider_prevents_admission(sql_scope, monkeypatch):
    enable(sql_scope, monkeypatch)
    def rollback():
        monkeypatch.setattr(get_settings(), "runtime_preference_enabled", False)
    instance, model, embedder = worker(sql_scope, [reply()], check=rollback)
    job_id = job(sql_scope, messages=[{"role": "user", "content": intent()["quote"]}])
    before = counts(sql_scope)
    with pytest.raises(PreferenceAdmissionUnavailable, match="rollout_unavailable"):
        instance.process(job_id)
    assert stored(sql_scope, job_id).state == "captured" and counts(sql_scope) == before
    assert len(model.calls) == 1 and not embedder.calls


def test_native_redis_strict_cache_delivery_removes_owned_keys(sql_scope):
    import asyncio
    import os

    from api.db.cache import CacheService

    async def exercise():
        cache = CacheService(redis_url=os.environ["REDIS_URL"], use_direct_breaker=True)
        owner, other = str(sql_scope.proxy_id), str(uuid.uuid4())
        keys = [f"retrieve:{owner}:{index}" for index in range(305)]
        keys += [cache._hot_memories_key(owner), cache._hot_tier_memory_key(owner, "synthetic")]
        safe_key = f"retrieve:{other}:synthetic"
        try:
            await cache.client.mset({key: "synthetic" for key in [*keys, safe_key]})
            await cache.invalidate_user_cache(owner, strict=True)
            assert not [key async for key in cache.client.scan_iter(f"retrieve:{owner}:*")]
            assert await cache.client.exists(*keys) == 0
            assert await cache.client.get(safe_key) == "synthetic"
        finally:
            await cache.client.delete(*keys, safe_key)
            await cache.client.aclose()
    asyncio.run(exercise())


def test_single_connection_pool_released_for_all_provider_and_cache_work(sql_scope, monkeypatch):
    seed(sql_scope, monkeypatch)
    old_fact = fact(sql_scope)
    enable(sql_scope, monkeypatch)
    engine = create_engine(sql_scope.factory.kw["bind"].url, pool_size=1, max_overflow=0, pool_timeout=2)
    factory = sessionmaker(engine, expire_on_commit=True)
    def check():
        assert engine.pool.checkedout() == 0
    instance, _, _ = worker(SimpleNamespace(factory=factory),
        [reply(nonpreferences=[FACT]), _decision(old_fact, relation="supersedes")], check=check)
    try:
        assert instance.process(job(sql_scope, messages=[{"role": "user", "content": TEXT}]))["memories_created"] == 2
    finally:
        engine.dispose()


def install_worker(scope, monkeypatch, instance):
    monkeypatch.setattr(extraction_tasks, "build_extraction_session_factory", lambda: scope.factory)
    monkeypatch.setattr(extraction_tasks, "_run_runtime_preference_job", instance.process)
    monkeypatch.setattr(extraction_tasks, "_set_job_status", lambda *_args: None)
    monkeypatch.setattr(extraction_tasks, "_wait_for_development_crash_barrier", lambda **_kwargs: False)
    def forbidden(*_args, **_kwargs):
        pytest.fail("captured runtime job must never enter legacy extraction")
    monkeypatch.setattr(extraction_tasks, "run_extraction_pipeline", forbidden)
    retries = []
    monkeypatch.setattr(extraction_tasks.process_extraction_job, "apply_async", lambda **kw: retries.append(kw))
    return retries


def test_queue_cache_failure_retries_receipt_even_when_both_flags_rollback(sql_scope, monkeypatch):
    seed(sql_scope, monkeypatch)
    enable(sql_scope, monkeypatch)
    instance, model, embedder = worker(sql_scope, [reply()], cache=Cache(failures=1))
    retries = install_worker(sql_scope, monkeypatch, instance)
    job_id = job(sql_scope, messages=[{"role": "user", "content": intent()["quote"]}])
    broker = {"job_id": str(job_id), "_payload_reference": "extraction_job", "queue_name": "starter-extraction"}
    failed = extraction_tasks._process_extraction_job(broker, celery_task_id=None)
    assert failed["status"] == "failed" and len(retries) == 1
    assert stored(sql_scope, job_id).state == "applied"
    before = counts(sql_scope)
    monkeypatch.setattr(get_settings(), "runtime_preference_enabled", False)
    monkeypatch.setattr(get_settings(), "explicit_preference_claims_enabled", False)
    done = extraction_tasks._process_extraction_job(broker, celery_task_id=None)
    assert done["status"] == "processed" and done["memories_created"] == 1
    assert counts(sql_scope) == before and len(model.calls) == len(embedder.calls) == 1
    with sql_scope.factory() as session:
        db_job = session.get(ExtractionJob, job_id)
        assert db_job.status == ExtractionJobStatus.completed
        assert db_job.result["stored_memories"] == done["stored_memories"]
    import asyncio
    import os

    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from api.errors import APIError
    from api.routers.memories import get_memory_job_status
    from api.services.memory_service import MemoryService

    async def status():
        engine = create_async_engine(os.environ["DATABASE_URL"])
        try:
            async with async_sessionmaker(engine, expire_on_commit=False)() as session:
                service = MemoryService.__new__(MemoryService)
                service.session = session
                request = SimpleNamespace(state=SimpleNamespace(tenant_id=str(sql_scope.tenant_id),
                    user_id=None, request_id="runtime-status"), headers={})
                response = await get_memory_job_status(request, job_id, service)
                assert response.data.created_memory_ids == [m["id"] for m in done["stored_memories"]]
                assert "snapshot" not in response.model_dump_json() and "quote" not in response.model_dump_json()
                request.state.tenant_id = str(uuid.uuid4())
                with pytest.raises(APIError) as foreign:
                    await get_memory_job_status(request, job_id, service)
                assert foreign.value.status_code == 404
        finally:
            await engine.dispose()
    asyncio.run(status())
    assert extraction_tasks._process_extraction_job(broker, celery_task_id=None)["status"] == "duplicate_ignored"


def test_queue_serialization_retry_reuses_fixed_classification(sql_scope, monkeypatch):
    seed(sql_scope, monkeypatch)
    enable(sql_scope, monkeypatch)
    instance, model, _ = worker(sql_scope, [reply()])
    retries = install_worker(sql_scope, monkeypatch, instance)
    apply = instance.transactions.apply
    attempts = []
    def conflict_once(**kwargs):
        attempts.append(kwargs)
        if len(attempts) == 1:
            error = Exception("controlled serialization failure")
            error.pgcode = "40001"
            raise DBAPIError(None, None, error)
        return apply(**kwargs)
    monkeypatch.setattr(instance.transactions, "apply", conflict_once)
    job_id = job(sql_scope, messages=[{"role": "user", "content": intent()["quote"]}])
    broker = {"job_id": str(job_id), "_payload_reference": "extraction_job"}
    failed = extraction_tasks._process_extraction_job(broker, celery_task_id=None)
    assert failed["error_type"] == "database_serialization_retry" and len(retries) == 1
    assert stored(sql_scope, job_id).state == "validated"
    assert extraction_tasks._process_extraction_job(broker, celery_task_id=None)["memories_created"] == 1
    assert len(model.calls) == 1 and attempts[0]["capture_id"] == attempts[1]["capture_id"]


@pytest.mark.parametrize("mutation", ["blocked", "inactive", "redacted", "changed_source", "foreign_payload"])
def test_applied_receipt_delivery_rechecks_permission_and_source(sql_scope, monkeypatch, mutation):
    seed(sql_scope, monkeypatch)
    enable(sql_scope, monkeypatch)
    instance, _, _ = worker(sql_scope, [reply()])
    job_id = job(sql_scope, messages=[{"role": "user", "content": intent()["quote"]}])
    instance.process(job_id)
    from datetime import UTC, datetime
    with sql_scope.factory.begin() as session:
        row = session.get(ExtractionJob, job_id)
        if mutation == "blocked":
            session.get(ProxyUser, sql_scope.proxy_id).is_blocked = True
        elif mutation == "inactive":
            session.get(Tenant, sql_scope.tenant_id).is_active = False
        elif mutation == "redacted":
            row.payload_redacted_at = datetime.now(UTC)
        elif mutation == "changed_source":
            row.payload = {**row.payload, "messages": [{"role": "user", "content": "changed"}]}
        else:
            row.payload = {**row.payload, "tenant_id": str(uuid.uuid4())}
    before = len(instance.cache_service.calls)
    with pytest.raises(PreferenceAdmissionUnavailable):
        instance.process(job_id)
    assert len(instance.cache_service.calls) == before
