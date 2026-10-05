"""Real admin HTTP, SQL locks and Redis admission; never paid model calls."""
import asyncio
import copy
import os
import threading
import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import event, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from api import dependencies
from api.db.cache import CacheService
from api.db.database import get_db_session
from api.db.models import (
    AuditLog,
    DeadLetterJob,
    ExtractionJob,
    ExtractionJobStatus,
    MemorySourceEvent,
    ProxyUser,
    Tenant,
)
from api.middleware.admin_auth import AdminAuthMiddleware
from api.routers import internal
from api.settings import get_settings
from api.tasks import provenance_tasks, watchdog_tasks
from api.tasks.queue_router import QueueRouter
from tests.integration.test_preference_choice_operations import counts
from tests.integration.test_preference_intent_admission import args
from tests.integration.test_runtime_pending_visibility import preference_context, read
from tests.integration.test_runtime_preference_ingestion import ingestion as _ingestion
from tests.integration.test_runtime_preference_worker import Cache, enable, seed, worker
from tests.integration.test_runtime_preference_worker import (
    forbid_real_providers as _forbid,
)
from tests.integration.test_runtime_preference_worker import sql_scope as _sql_scope
from tests.unit.test_preference_intent_contract import intent, reply

sql_scope = _sql_scope
ingestion = _ingestion
forbid_real_providers = _forbid
ADMIN = "isolated-recovery-admin-credential-not-real"


@pytest.fixture
def recovery_api(monkeypatch):
    dispatched = []
    fail_dispatch = []

    def dispatch(name, **kwargs):
        if fail_dispatch:
            raise ConnectionError("controlled broker outage")
        dispatched.append((name, kwargs))
        return SimpleNamespace(id=kwargs.get("task_id"))

    monkeypatch.setattr(internal.celery_app, "send_task", dispatch)

    @asynccontextmanager
    async def client():
        engine = create_async_engine(os.environ["DATABASE_URL"], pool_size=2, max_overflow=0)
        sessions = async_sessionmaker(engine, expire_on_commit=True)
        cache = CacheService(redis_url=os.environ["REDIS_URL"], use_direct_breaker=True)
        async def db():
            async with sessions() as session:
                yield session
        app = FastAPI()
        app.include_router(internal.router)
        app.add_middleware(AdminAuthMiddleware, admin_secret=ADMIN)
        app.dependency_overrides.update({get_db_session: db, dependencies.get_cache_service: lambda: cache})
        try:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
                                        base_url="http://test", headers={"X-Admin-Secret": ADMIN,
                                        "X-Request-ID": "recovery-check"}) as http:
                yield http
        finally:
            await cache.client.aclose()
            await engine.dispose()

    def call(job_id, *, method="POST", reason="Retry after fixing the controlled outage", authorized=True):
        async def exercise():
            async with client() as http:
                path = f"/v1/internal/dead-letter-jobs/{job_id}" + ("/retry" if method == "POST" else "")
                return await http.request(method, path, params={"reason": reason} if reason is not None else {},
                    headers={} if authorized else {"X-Admin-Secret": "not-authorized"})
        return asyncio.run(exercise())

    return SimpleNamespace(client=client, call=call, dispatched=dispatched, fail_dispatch=fail_dispatch)


def mark_dead(scope, job_id):
    with scope.factory.begin() as session:
        job = session.get(ExtractionJob, job_id)
        job.status = ExtractionJobStatus.dead
        job.attempts = 3
        job.celery_task_id = None
        job.error = "controlled_failure"
        job.queued_at = datetime.now(UTC) - timedelta(days=1)
        session.add(DeadLetterJob(job_id=job_id, tenant_id=scope.tenant_id, proxy_user_id=scope.proxy_id,
                                  payload=copy.deepcopy(job.payload), error=job.error))
        queue = job.queue_name
    async def release():
        cache = CacheService(redis_url=os.environ["REDIS_URL"], use_direct_breaker=True)
        try:
            await QueueRouter(session=None, cache_service=cache).release_extraction_slot(
                tenant_id=str(scope.tenant_id), queue_name=queue, job_id=str(job_id))
        finally:
            await cache.client.aclose()
    asyncio.run(release())


def prepare(scope, ingestion, monkeypatch, state="uncaptured"):
    old = seed(scope, monkeypatch)
    enable(scope, monkeypatch)
    job_id = uuid.UUID(ingestion.send(messages=[{"role": "user", "content": intent()["quote"]}])["job_id"])
    instance, model, embedder = worker(scope, [reply()], cache=Cache(failures=1 if state == "applied" else 0))
    if state in {"captured", "validated"}:
        captured = instance.transactions.capture(**args(scope, job_id))
        if state == "validated":
            instance.transactions.admit(reply(), llm_service=model, **args(scope, job_id), capture_id=captured.capture_id)
    elif state == "applied":
        with pytest.raises(ConnectionError, match="cache outage"):
            instance.process(job_id)
    mark_dead(scope, job_id)
    return old, job_id, instance, model, embedder


def saved(scope, job_id):
    with scope.factory() as session:
        job = session.get(ExtractionJob, job_id)
        return copy.deepcopy({"route": job.runtime_route, "applied": job.runtime_applied_at,
            "record": job.preference_intent_admission, "source": job.payload.get("source"),
            "messages": job.payload.get("messages"), "count": job.memories_created})


def retry_audits(scope):
    with scope.factory() as session:
        return list(session.scalars(select(AuditLog).where(AuditLog.proxy_user_id == scope.proxy_id,
            AuditLog.metadata_json["event"].astext == "runtime_job_retry_requested")))


@pytest.mark.parametrize("historical", [False, True])
def test_discard_never_removes_reserved_or_old_capture_guard(sql_scope, ingestion, monkeypatch, recovery_api, historical):
    old, job_id, _, _, _ = prepare(sql_scope, ingestion, monkeypatch, "captured" if historical else "uncaptured")
    if historical:
        with sql_scope.factory.begin() as session:
            session.get(ExtractionJob, job_id).runtime_route = None
    before = saved(sql_scope, job_id)
    response = recovery_api.call(job_id, method="DELETE")
    assert response.status_code == 409 and response.json()["detail"] == "runtime_job_discard_forbidden"
    assert saved(sql_scope, job_id) == before
    assert read(sql_scope, [old])[1].state == "unavailable"
    assert recovery_api.dispatched == []


@pytest.mark.parametrize("state", ["uncaptured", "captured", "validated", "applied"])
def test_retry_preserves_fixed_record_and_atomic_applied_projection(sql_scope, ingestion, monkeypatch, recovery_api, state):
    old, job_id, instance, model, embedder = prepare(sql_scope, ingestion, monkeypatch, state)
    before = saved(sql_scope, job_id)
    if state == "applied":
        monkeypatch.setattr(get_settings(), "runtime_preference_enabled", False)
        monkeypatch.setattr(get_settings(), "explicit_preference_claims_enabled", False)
    response = recovery_api.call(job_id)
    assert response.status_code == 200, response.text
    assert saved(sql_scope, job_id) == before
    audit = retry_audits(sql_scope)
    assert len(audit) == 1
    assert audit[0].metadata_json["actor_credential_class"] == "admin_secret"
    assert audit[0].metadata_json["request_id"] == "recovery-check"
    assert audit[0].metadata_json["record_state"] == state
    assert "messages" not in audit[0].metadata_json and "human" not in audit[0].metadata_json
    name, dispatch = recovery_api.dispatched[0]
    assert name == internal.EXTRACTION_TASK_NAME
    assert dispatch["args"] == [{"job_id": str(job_id), "tenant_id": str(sql_scope.tenant_id),
                               "queue_name": "starter-extraction", "_payload_reference": "extraction_job"}]
    with sql_scope.factory() as session:
        row = session.get(ExtractionJob, job_id)
        assert row.celery_task_id == dispatch["task_id"] and row.attempts == 0
        assert row.queued_at > datetime.now(UTC) - timedelta(minutes=1)
    visibility = read(sql_scope, [old], context=preference_context())[1]
    assert (visibility is None) == (state == "applied")
    if state in {"validated", "applied"}:
        before_counts, calls = counts(sql_scope), (len(model.calls), len(embedder.calls))
        instance.process(job_id)
        assert len(model.calls) == calls[0]  # A fixed decision is never reclassified.
        if state == "applied":
            assert counts(sql_scope) == before_counts and len(embedder.calls) == calls[1]
        items, visibility, _ = read(sql_scope, [old], context=preference_context())
        assert visibility is None and len(items) == 1 and "Python" in items[0].content


@pytest.mark.parametrize("change", ["expired", "redacted", "transcript", "source", "catalog", "owner", "tenant", "rollback", "route"])
def test_invalid_recovery_is_rejected_before_queue_or_provider(sql_scope, ingestion, monkeypatch, recovery_api, change):
    old, job_id, _, model, embedder = prepare(sql_scope, ingestion, monkeypatch, "validated")
    with sql_scope.factory.begin() as session:
        job = session.get(ExtractionJob, job_id)
        if change == "expired":
            job.raw_payload_expires_at = datetime.now(UTC) - timedelta(seconds=1)
        elif change == "redacted":
            job.payload_redacted_at = datetime.now(UTC)
        elif change == "transcript":
            job.payload = {**job.payload, "messages": [{"role": "user", "content": "changed"}]}
        elif change == "source":
            source = session.get(MemorySourceEvent, job.source_event_id)
            source.payload_hash = "f" * 64
        elif change == "catalog":
            raw = copy.deepcopy(job.preference_intent_admission)
            raw["catalog_sha256"] = "f" * 64
            job.preference_intent_admission = raw
        elif change == "owner":
            session.get(ProxyUser, sql_scope.proxy_id).is_blocked = True
        elif change == "tenant":
            session.get(Tenant, sql_scope.tenant_id).is_active = False
        elif change == "route":
            job.runtime_route = "unknown-version"
    if change == "rollback":
        monkeypatch.setattr(get_settings(), "runtime_preference_enabled", False)
    before = saved(sql_scope, job_id)
    response = recovery_api.call(job_id)
    assert response.status_code == 409, response.text
    assert saved(sql_scope, job_id) == before
    assert not retry_audits(sql_scope) and not recovery_api.dispatched and not model.calls and not embedder.calls
    with sql_scope.factory() as session:
        assert session.get(ExtractionJob, job_id).status == ExtractionJobStatus.dead
    if change not in {"owner", "tenant"}:
        assert read(sql_scope, [old])[1].state == "unavailable"


def test_concurrent_retry_has_one_transition_dispatch_and_audit(sql_scope, ingestion, monkeypatch, recovery_api):
    _, job_id, _, _, _ = prepare(sql_scope, ingestion, monkeypatch)
    async def exercise():
        async with recovery_api.client() as http:
            path = f"/v1/internal/dead-letter-jobs/{job_id}/retry"
            return await asyncio.gather(*(http.post(path, params={"reason": "controlled concurrent retry"}) for _ in range(2)))
    responses = asyncio.run(exercise())
    codes = sorted(r.status_code for r in responses)
    assert codes in ([200, 409], [200, 404]), [r.text for r in responses]
    assert len(recovery_api.dispatched) == len(retry_audits(sql_scope)) == 1


def test_busy_job_rejects_retry_and_discard_without_waiting(sql_scope, ingestion, monkeypatch, recovery_api):
    _, job_id, _, _, _ = prepare(sql_scope, ingestion, monkeypatch)
    with sql_scope.factory.begin() as session:
        session.get(ExtractionJob, job_id, with_for_update=True)
        for method in ("POST", "DELETE"):
            response = recovery_api.call(job_id, method=method)
            assert response.status_code == 409 and response.json()["detail"] == "dead_letter_job_busy"
    assert not recovery_api.dispatched and not retry_audits(sql_scope)


def test_dispatch_ack_never_replaces_watchdog_lease(sql_scope, ingestion, monkeypatch, recovery_api):
    _, job_id, _, _, _ = prepare(sql_scope, ingestion, monkeypatch, "validated")
    deliveries = []
    # Replace the module reference: Celery's shared-task proxy resolves against
    # a thread-local app, so patching its resolved task misses the broker thread.
    monkeypatch.setattr(watchdog_tasks, "process_extraction_job",
                        SimpleNamespace(apply_async=lambda **kw: deliveries.append(kw)))
    def delayed_ack(_name, **_kwargs):
        # API already committed and released SQL before attempting dispatch.
        with sql_scope.factory.begin() as session:
            row = session.get(ExtractionJob, job_id, with_for_update={"nowait": True})
            row.queued_at = datetime.now(UTC) - timedelta(seconds=61)
        assert watchdog_tasks.run_watchdog_cycle(session_factory=sql_scope.factory)["requeued"] == 1
        return SimpleNamespace(id=_kwargs["task_id"])
    monkeypatch.setattr(internal.celery_app, "send_task", delayed_ack)
    response = recovery_api.call(job_id)
    assert response.status_code == 200, response.text
    with sql_scope.factory() as session:
        assert session.get(ExtractionJob, job_id).celery_task_id == deliveries[0]["task_id"]
    assert len(retry_audits(sql_scope)) == 1


def test_blocking_broker_does_not_block_api_event_loop(sql_scope, ingestion, monkeypatch, recovery_api):
    _, job_id, _, _, _ = prepare(sql_scope, ingestion, monkeypatch)
    entered, release = threading.Event(), threading.Event()
    def broker(_name, **kwargs):
        entered.set()
        if not release.wait(timeout=5):
            raise AssertionError("API event loop could not release blocked broker")
        return SimpleNamespace(id=kwargs["task_id"])
    monkeypatch.setattr(internal.celery_app, "send_task", broker)
    async def exercise():
        async with recovery_api.client() as http:
            task = asyncio.create_task(http.post(f"/v1/internal/dead-letter-jobs/{job_id}/retry",
                                                  params={"reason": "controlled broker check"}))
            try:
                assert await asyncio.wait_for(asyncio.to_thread(entered.wait, 5), timeout=6)
                release.set()  # Runs on the same API event loop while broker blocks.
                return await task
            finally:
                release.set()
    assert asyncio.run(exercise()).status_code == 200


def test_broker_failure_leaves_guard_and_existing_watchdog_can_dispatch(sql_scope, ingestion, monkeypatch, recovery_api):
    old, job_id, _, _, _ = prepare(sql_scope, ingestion, monkeypatch, "validated")
    before = saved(sql_scope, job_id)
    recovery_api.fail_dispatch.append(True)
    response = recovery_api.call(job_id)
    assert response.status_code == 503 and response.json()["detail"]["status"] == "queued"
    assert saved(sql_scope, job_id) == before and len(retry_audits(sql_scope)) == 1
    assert read(sql_scope, [old])[1].state == "processing"
    with sql_scope.factory.begin() as session:
        row = session.get(ExtractionJob, job_id)
        assert row.celery_task_id is None
        row.queued_at = datetime.now(UTC) - timedelta(seconds=61)
    deliveries = []
    monkeypatch.setattr(watchdog_tasks.process_extraction_job, "apply_async", lambda **kw: deliveries.append(kw))
    assert watchdog_tasks.run_watchdog_cycle(session_factory=sql_scope.factory)["requeued"] == 1
    assert watchdog_tasks.run_watchdog_cycle(session_factory=sql_scope.factory)["requeued"] == 0
    assert len(deliveries) == 1 and saved(sql_scope, job_id) == before


def test_audit_failure_rolls_back_retry_and_releases_queue_slot(sql_scope, ingestion, monkeypatch, recovery_api):
    _, job_id, _, _, _ = prepare(sql_scope, ingestion, monkeypatch)
    def reject_audit(_mapper, _connection, audit):
        if audit.metadata_json.get("event") == "runtime_job_retry_requested":
            raise RuntimeError("controlled audit persistence failure")
    event.listen(AuditLog, "before_insert", reject_audit)
    try:
        assert recovery_api.call(job_id).status_code == 500
    finally:
        event.remove(AuditLog, "before_insert", reject_audit)
    with sql_scope.factory() as session:
        assert session.get(ExtractionJob, job_id).status == ExtractionJobStatus.dead
    assert not retry_audits(sql_scope) and not recovery_api.dispatched
    async def check_slot():
        cache = CacheService(redis_url=os.environ["REDIS_URL"], use_direct_breaker=True)
        try:
            assert await cache.client.zscore("queue_depth:starter-extraction:jobs", f"{sql_scope.tenant_id}:{job_id}") is None
        finally:
            await cache.client.aclose()
    asyncio.run(check_slot())


def test_retention_cannot_be_recovered_by_discard_or_retry(sql_scope, ingestion, monkeypatch, recovery_api):
    old, job_id, _, _, _ = prepare(sql_scope, ingestion, monkeypatch, "captured")
    with sql_scope.factory.begin() as session:
        row = session.get(ExtractionJob, job_id)
        row.runtime_route = None
        row.raw_payload_expires_at = datetime.now(UTC) - timedelta(seconds=1)
    monkeypatch.setattr(provenance_tasks, "build_sync_session_factory", lambda: sql_scope.factory)
    assert provenance_tasks.redact_expired_extraction_payloads.run()["redacted_jobs"] == 1
    assert recovery_api.call(job_id).status_code == 409
    assert recovery_api.call(job_id, method="DELETE").status_code == 409
    assert read(sql_scope, [old])[1].state == "unavailable"
    assert not recovery_api.dispatched


def test_admin_auth_and_reason_are_required(sql_scope, ingestion, monkeypatch, recovery_api):
    _, job_id, _, _, _ = prepare(sql_scope, ingestion, monkeypatch)
    assert recovery_api.call(job_id, authorized=False).status_code == 403
    for reason in (None, "  ", "x" * 513):
        assert recovery_api.call(job_id, reason=reason).status_code == 422
    assert not recovery_api.dispatched and not retry_audits(sql_scope)


def test_legacy_retry_and_discard_remain_supported(sql_scope, ingestion, monkeypatch, recovery_api):
    monkeypatch.setattr(get_settings(), "runtime_preference_enabled", False)
    job_id = uuid.UUID(ingestion.send()["job_id"])
    mark_dead(sql_scope, job_id)
    response = recovery_api.call(job_id, reason=None)
    assert response.status_code == 200 and not retry_audits(sql_scope)
    assert "messages" in recovery_api.dispatched[0][1]["args"][0]
    other = uuid.UUID(ingestion.send()["job_id"])
    mark_dead(sql_scope, other)
    assert recovery_api.call(other, method="DELETE").status_code == 200
    with sql_scope.factory() as session:
        assert session.get(ExtractionJob, other) is None
