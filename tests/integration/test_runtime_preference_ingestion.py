"""Real API-key auth -> normal add -> durable queue -> runtime -> governed SQL read.

Only quality/provider results and broker delivery are controlled. Authentication,
proxy identity, provenance, Redis admission, SQL writes and read selection are real.
"""
import asyncio
import os
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from api import dependencies
from api.db.cache import CacheService
from api.db.models import (
    ApiKey,
    ExtractionJob,
    Memory,
    MemorySourceEvent,
    PendingExtractionCandidate,
    ProxyUser,
    QuotaMode,
    ServiceWriter,
)
from api.dependencies import DbSession
from api.middleware.auth import AuthMiddleware
from api.routers import memories
from api.services.memory_service import MemoryService
from api.services.preference_intent_admission_service import (
    PreferenceAdmissionUnavailable,
)
from api.services.preference_intent_transaction import (
    PreferenceIntentTransactionService,
)
from api.services.proxy_user_service import ProxyUserService
from api.services.quality_gate import GateResult
from api.utils.crypto import api_key_prefix, hash_api_key
from tests.integration.test_preference_choice_policy import read_decisions
from tests.integration.test_preference_fact_transaction import fact
from tests.integration.test_preference_intent_admission import args
from tests.integration.test_preference_intent_transaction import FACT, TEXT
from tests.integration.test_runtime_preference_worker import (
    Cache,
    enable,
    install_worker,
    seed,
    worker,
)
from tests.integration.test_runtime_preference_worker import (
    forbid_real_providers as _forbid,
)
from tests.integration.test_runtime_preference_worker import (
    sql_scope as _sql_scope,
)
from tests.integration.test_source_decision_postgres import _decision
from tests.unit.test_preference_intent_contract import intent, reply

sql_scope = _sql_scope
forbid_real_providers = _forbid


@pytest.fixture
def ingestion(sql_scope, monkeypatch):
    raw_key = f"ingestion-test-{uuid.uuid4()}"
    key_id = uuid.uuid4()
    with sql_scope.factory.begin() as session:
        session.add(ApiKey(id=key_id, tenant_id=sql_scope.tenant_id, key_hash=hash_api_key(raw_key),
                           key_prefix=api_key_prefix(raw_key), name="isolated ingestion test",
                           permissions=["memory:write", "memory:read"]))
    dispatched = []
    monkeypatch.setattr(memories, "get_processing_eta", lambda _: None)

    async def send_async(body, *, authorized=True, trusted_submission_kind=None):
        engine = create_async_engine(os.environ["DATABASE_URL"])
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        cache = CacheService(redis_url=os.environ["REDIS_URL"], use_direct_breaker=True)
        quota = SimpleNamespace(get_quota_envelope=AsyncMock(return_value=SimpleNamespace(
            mode=QuotaMode.full, budget_remaining_pct=1.0)))
        async def db():
            async with sessions() as session:
                yield session
        async def proxies(session: DbSession):
            return ProxyUserService(session=session, cache_service=cache)
        async def service(session: DbSession):
            return MemoryService(session=session, cache_service=cache, qdrant_service=SimpleNamespace(),
                quota_manager=quota, embedding_service=SimpleNamespace(),
                dispatch_task=lambda _name, **kw: dispatched.append(kw["args"][0]))
        app = FastAPI()
        app.include_router(memories.router)
        app.dependency_overrides.update({dependencies.get_db_session: db,
            dependencies.get_memory_service: service, dependencies.get_proxy_user_service: proxies,
            dependencies.get_quality_gate_service: lambda: SimpleNamespace(check=AsyncMock(
                return_value=GateResult(passed=True, blocked_layer=None, reason=None, budget_remaining_pct=1.0)))})
        async with httpx.AsyncClient() as auth_http:
            app.add_middleware(AuthMiddleware, session_factory=sessions, redis_client=cache.client, http_client=auth_http)
            try:
                if trusted_submission_kind is not None:
                    # Internal MCP adapter supplies this argument, never public metadata.
                    async with sessions() as session:
                        svc = await service(session)
                        queued = await svc.queue_memory_add(requested_user_id=None, authenticated_user_id=None,
                            agent_id=None, tenant_id=str(sql_scope.tenant_id), proxy_user_id=str(sql_scope.proxy_id),
                            external_user_id=sql_scope.external_id, api_key_id=str(key_id), messages=body["messages"],
                            metadata=body.get("metadata", {}), idempotency_key=None, evidence_mode="client_assertion",
                            trusted_submission_kind=trusted_submission_kind)
                        return {"status": "queued", "job_id": queued["job_id"]}
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
                    response = await client.post("/v1/memories/add", json=body,
                        headers={"Authorization": f"ApiKey {raw_key}"} if authorized else {})
                    assert response.status_code == (200 if authorized else 401), response.text
                    return response.json()
            finally:
                await cache.client.aclose()
                await engine.dispose()

    def send(**changes):
        authorized = changes.pop("authorized", True)
        kind = changes.pop("trusted_submission_kind", None)
        body = {"external_user_id": sql_scope.external_id, "conversation_id": "ingestion-boundary",
                "messages": [{"role": "user", "content": TEXT}], **changes}
        return asyncio.run(send_async(body, authorized=authorized, trusted_submission_kind=kind))
    return SimpleNamespace(send=send, key_id=key_id, dispatched=dispatched)


def test_authenticated_normal_add_reaches_runtime_and_keeps_event_provenance(sql_scope, ingestion, monkeypatch):
    old_preference = seed(sql_scope, monkeypatch)
    old_fact = fact(sql_scope)
    enable(sql_scope, monkeypatch)
    ingestion.send(authorized=False)
    assert ingestion.dispatched == []
    accepted = ingestion.send(metadata={"conversation_ingestion_sha256": "forged-public-marker"})
    job_id = uuid.UUID(accepted["job_id"])
    with sql_scope.factory() as session:
        row = session.get(ExtractionJob, job_id)
        event = session.get(MemorySourceEvent, row.source_event_id)
        assert event.writer_id is None and event.api_key_id == ingestion.key_id
        assert event.processing_metadata["conversation_ingestion_sha256"] != "forged-public-marker"
        assert row.payload["source_event_id"] == str(event.id)
        event_id = event.id
    instance, model, embedder = worker(sql_scope, [reply(nonpreferences=[FACT]), _decision(old_fact, relation="supersedes")])
    install_worker(sql_scope, monkeypatch, instance)
    broker = ingestion.dispatched[0]
    assert "messages" not in broker and broker["_payload_reference"] == "extraction_job"
    from api.tasks.extraction_tasks import _process_extraction_job
    result = _process_extraction_job(broker, celery_task_id=None)
    assert result["status"] == "processed" and result["memories_created"] == 2
    with sql_scope.factory() as session:
        rows = list(session.scalars(select(Memory).where(Memory.id.in_(
            [uuid.UUID(item["id"]) for item in result["stored_memories"]]))))
        assert session.get(Memory, old_preference).is_archived and session.get(Memory, old_fact).is_archived
        for row in rows:
            provenance = row.metadata_json["provenance"]
            assert row.source_event_id == event_id and provenance["source_event_id"] == str(event_id)
            assert provenance["attestation"] == "client_asserted" and provenance["authority_priority"] == 20
            assert provenance["authority_rules"]["default_priority"] == 20
            assert provenance["external_conversation_id"] == "ingestion-boundary"
            assert provenance["writer_id"] is None
    current, decisions = read_decisions(sql_scope, {"kind": "global"})
    assert len(current) == 1 and "Python" in current[0].content and not decisions
    assert _process_extraction_job(broker, celery_task_id=None)["status"] == "duplicate_ignored"
    assert len(model.calls) == len(embedder.calls) == 2


@pytest.mark.parametrize("kind", ["registered", "explicit", "client_assertion", "mcp", "historical"])
def test_normal_ingestion_does_not_promote_other_source_paths(sql_scope, ingestion, monkeypatch, kind):
    enable(sql_scope, monkeypatch)
    changes = {}
    if kind in {"registered", "explicit"}:
        with sql_scope.factory.begin() as session:
            session.add(ServiceWriter(tenant_id=sql_scope.tenant_id, api_key_id=ingestion.key_id,
                service_key="registered-test", display_name="Registered", authority_rules={"default_priority": 80}))
        if kind == "explicit":
            changes["source"] = {"service": "registered-test", "event_id": "event-test",
                                 "observed_at": datetime.now(UTC).isoformat()}
    if kind == "client_assertion":
        changes.update(evidence_mode="client_assertion", metadata={"conversation_ingestion_sha256": "forged"})
    if kind == "mcp":
        changes["trusted_submission_kind"] = "mcp_client_assertion"
    accepted = ingestion.send(**changes)
    job_id = uuid.UUID(accepted["job_id"])
    with sql_scope.factory.begin() as session:
        row = session.get(ExtractionJob, job_id)
        event = session.get(MemorySourceEvent, row.source_event_id)
        if kind == "historical":
            row.runtime_route = None  # Genuinely old, unreserved job.
            event.processing_metadata = {k: v for k, v in event.processing_metadata.items()
                                         if k != "conversation_ingestion_sha256"}
        assert "conversation_ingestion_sha256" not in event.processing_metadata
    instance, model, embedder = worker(sql_scope)
    assert instance.process(job_id) is None
    assert not model.calls and not embedder.calls
    # Standalone experimental transaction gates remain closed even for normal sources.
    with pytest.raises(PreferenceAdmissionUnavailable, match="source_ineligible"):
        PreferenceIntentTransactionService(sql_scope.factory).capture(**args(sql_scope, job_id))


def test_normal_source_does_not_open_standalone_transaction_or_disabled_rollout(sql_scope, ingestion, monkeypatch):
    from api.settings import get_settings
    enable(sql_scope, monkeypatch)
    job_id = uuid.UUID(ingestion.send()["job_id"])
    with pytest.raises(PreferenceAdmissionUnavailable, match="registered_source_ineligible"):
        PreferenceIntentTransactionService(sql_scope.factory).capture(**args(sql_scope, job_id))
    monkeypatch.setattr(get_settings(), "runtime_preference_enabled", False)
    instance, model, embedder = worker(sql_scope)
    with pytest.raises(PreferenceAdmissionUnavailable, match="intent_rollout_unavailable"):
        instance.process(job_id)
    assert not model.calls and not embedder.calls


def test_normal_pending_review_keeps_source_event_without_activating_tentative_text(sql_scope, ingestion, monkeypatch):
    seed(sql_scope, monkeypatch)
    enable(sql_scope, monkeypatch)
    quote = "Maybe Python should be my default; I have not decided."
    job_id = uuid.UUID(ingestion.send(messages=[{"role": "user", "content": quote}])["job_id"])
    instance, _, embedder = worker(sql_scope, [reply(preferences=[intent(action="tentative", quote=quote)])])
    result = instance.process(job_id)
    assert result["memories_created"] == 0 and not embedder.calls
    with sql_scope.factory() as session:
        candidate = session.scalar(select(PendingExtractionCandidate).where(
            PendingExtractionCandidate.extraction_job_id == job_id))
        assert candidate is not None
        assert candidate.source_event_id == session.get(ExtractionJob, job_id).source_event_id
    current, _ = read_decisions(sql_scope, {"kind": "global"})
    assert len(current) == 1 and "C++" in current[0].content


def test_normal_receipt_retry_under_rollback_does_not_reclassify(sql_scope, ingestion, monkeypatch):
    from api.settings import get_settings
    from api.tasks.extraction_tasks import _process_extraction_job
    seed(sql_scope, monkeypatch)
    enable(sql_scope, monkeypatch)
    accepted = ingestion.send(messages=[{"role": "user", "content": intent()["quote"]}])
    cache = Cache(failures=1)
    instance, model, embedder = worker(sql_scope, [reply()], cache=cache)
    retries = install_worker(sql_scope, monkeypatch, instance)
    broker = ingestion.dispatched[0]
    assert _process_extraction_job(broker, celery_task_id=None)["status"] == "failed"
    assert len(retries) == 1
    monkeypatch.setattr(get_settings(), "runtime_preference_enabled", False)
    monkeypatch.setattr(get_settings(), "explicit_preference_claims_enabled", False)
    completed = _process_extraction_job(broker, celery_task_id=None)
    assert completed["status"] == "processed" and completed["memories_created"] == 1
    assert len(model.calls) == len(embedder.calls) == 1
    with sql_scope.factory() as session:
        row = session.get(ExtractionJob, uuid.UUID(accepted["job_id"]))
        assert row.preference_intent_admission["runtime_source_sha256"] is not None
        assert row.result["stored_memories"] == completed["stored_memories"]


@pytest.mark.parametrize("key,value", [("agent_id", "forged-agent"),
                                      ("source", {"explicit": True}), ("messages", [])])
def test_changed_marked_job_cannot_fall_back_before_capture(sql_scope, ingestion, monkeypatch, key, value):
    enable(sql_scope, monkeypatch)
    job_id = uuid.UUID(ingestion.send()["job_id"])
    with sql_scope.factory.begin() as session:
        row = session.get(ExtractionJob, job_id)
        row.payload = {**row.payload, key: value}
    instance, model, embedder = worker(sql_scope)
    with pytest.raises(PreferenceAdmissionUnavailable):
        instance.process(job_id)
    assert not model.calls and not embedder.calls


@pytest.mark.parametrize("mutation", ["transcript", "policy", "owner", "writer", "hash", "observed_at",
                                       "payload_event", "agent", "explicit", "removed_event", "removed_marker"])
def test_persisted_source_binding_changes_fail_before_provider(sql_scope, ingestion, monkeypatch, mutation):
    enable(sql_scope, monkeypatch)
    job_id = uuid.UUID(ingestion.send()["job_id"])
    instance, model, embedder = worker(sql_scope)
    instance.transactions.capture(**args(sql_scope, job_id))
    with sql_scope.factory.begin() as session:
        row = session.get(ExtractionJob, job_id)
        event = session.get(MemorySourceEvent, row.source_event_id)
        if mutation == "owner":
            other_id = uuid.uuid4()
            external_id = f"other-{other_id}"
            session.add(ProxyUser(id=other_id, tenant_id=sql_scope.tenant_id, external_user_id=external_id,
                external_user_id_hash=ProxyUserService.hash_external_user_id(str(sql_scope.tenant_id), external_id),
                metadata_json={}))
            session.flush()
            event.proxy_user_id = other_id
        elif mutation == "writer":
            writer = ServiceWriter(tenant_id=sql_scope.tenant_id, service_key="tamper", display_name="Tamper", authority_rules={})
            session.add(writer)
            session.flush()
            event.writer_id = writer.id
        elif mutation == "hash":
            event.payload_hash = "f" * 64
        elif mutation == "observed_at":
            event.observed_at += timedelta(seconds=1)
        elif mutation == "removed_marker":
            event.processing_metadata = {}
        else:
            payload = dict(row.payload)
            if mutation == "transcript":
                payload["messages"] = [{"role": "user", "content": "Use Rust"}]
            elif mutation == "policy":
                payload["evidence_policy"] = {**payload["evidence_policy"], "authority_priority": 90}
            elif mutation == "explicit":
                payload["source"] = {**payload["source"], "explicit": True}
            elif mutation == "agent":
                payload["agent_id"] = str(uuid.uuid4())
            elif mutation == "removed_event":
                row.source_event_id = None
                payload.pop("source_event_id")
            else:
                payload["source_event_id"] = str(uuid.uuid4())
            row.payload = payload
    with pytest.raises(PreferenceAdmissionUnavailable):
        instance.process(job_id)
    assert not model.calls and not embedder.calls
