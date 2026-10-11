"""Disposable local PostgreSQL/Redis only; never use configured deployment DSNs."""
import importlib
import json
import socket
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from threading import Barrier, Event
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import redis
from starlette.requests import Request
from starlette.responses import Response
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, delete, event, func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from api.db.cache import CacheService
from api.db.models import (ApiKey, AuditLog, Base, EmbeddingModel, EmbeddingProvider,
    ExtractionJob, ExtractionJobStatus, Memory, MemoryClaim, MemoryClaimRevision,
    MemorySourceEvent, PlanTier, ProxyUser, Region, Tenant, VectorSyncOutbox)
from api.errors import APIError
from api.schemas.requests import MemoryAddRequest
from api.services import memory_service as service_module
from api.routers import memories as memory_routes
from api.services.memory_add_replay import memory_add_replay_binding
from api.services.memory_service import MemoryService
from api.services.proxy_user_service import ProxyUserService
from api.services.embedding_service import EmbeddingResult, DEFAULT_ACTIVE_MODEL_ID
from api.settings import get_settings
from api.tasks import extraction_tasks, provenance_tasks
from api.tasks.queue_router import _tenant_queue_depth_key, _queue_jobs_key

PG_URL = "postgresql+psycopg2://replay_test:replay_test_only@127.0.0.1:55471/replay_test"
REDIS_URL = "redis://127.0.0.1:55472/0"


def run(coroutine):
    try:
        coroutine.send(None)
    except StopIteration as done:
        return done.value
    else:
        raise AssertionError("unexpected suspension; no event loop/external work scheduled")
    finally:
        coroutine.close()


class SessionAdapter:
    """Real SQL/transactions with synchronous completion of async boundaries."""
    def __init__(self, session):
        self.sync = session

    async def execute(self, *args, **kwargs):
        return self.sync.execute(*args, **kwargs)

    async def get(self, *args, **kwargs):
        return self.sync.get(*args, **kwargs)

    async def flush(self):
        self.sync.flush()

    async def commit(self):
        self.sync.commit()

    async def rollback(self):
        self.sync.rollback()

    def add(self, obj):
        self.sync.add(obj)


class RedisAdapter:
    def __init__(self, client):
        self.sync = client

    async def get(self, *args, **kwargs):
        return self.sync.get(*args, **kwargs)

    async def set(self, *args, **kwargs):
        return self.sync.set(*args, **kwargs)

    async def eval(self, *args, **kwargs):
        return self.sync.eval(*args, **kwargs)

    async def delete(self, *args, **kwargs):
        return self.sync.delete(*args, **kwargs)


class Forbidden:
    def __getattr__(self, name):
        raise AssertionError(f"provider/unexpected dependency prohibited: {name}")


@pytest.fixture(scope="module")
def database():
    # Deliberately fixed fresh container ports; no .env or configured DB lookup.
    engine = create_engine(PG_URL, pool_size=8, connect_args={"connect_timeout": 3})
    schema = "replay_" + uuid.uuid4().hex
    with engine.begin() as connection:
        connection.execute(text(f'CREATE SCHEMA "{schema}"'))

    @event.listens_for(engine, "connect")
    def path(dbapi_connection, _record):
        with dbapi_connection.cursor() as cursor:
            cursor.execute(f'SET search_path TO "{schema}"')
        dbapi_connection.commit()

    engine.dispose()
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    with factory.begin() as session:
        session.add(Region(id="IN1", aws_region="local-test", postgres_url_secret="unused",
            qdrant_url_secret="unused", redis_url_secret="unused"))
        session.add(EmbeddingModel(id=DEFAULT_ACTIVE_MODEL_ID, provider=EmbeddingProvider.openai,
            model_name="controlled", dimensions=1536, qdrant_collection="memories", is_active=True))
    try:
        yield SimpleNamespace(engine=engine, factory=factory, schema=schema)
    finally:
        # Only the random schema in the explicitly named disposable DB is removed.
        with engine.begin() as connection:
            connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        engine.dispose()


@pytest.fixture
def scope(database, monkeypatch):
    original_connect = socket.socket.connect

    def local_only(sock, address):
        if not isinstance(address, tuple) or address[0] not in {"127.0.0.1", "localhost"} or address[1] not in {55471, 55472}:
            raise AssertionError("non-test network prohibited")
        return original_connect(sock, address)

    monkeypatch.setattr(socket.socket, "connect", local_only)
    controlled = get_settings().model_copy(update={"explicit_preference_claims_enabled": True,
        "runtime_preference_enabled": False, "phase3a_confirmation_enabled": False,
        "phase3a_confirmation_shadow_enabled": False})
    # Test-process settings objects only; no singleton/config/environment flag changes.
    monkeypatch.setattr(service_module, "get_settings", lambda: controlled)
    monkeypatch.setattr(extraction_tasks, "get_settings", lambda: controlled)
    monkeypatch.setattr(memory_routes, "get_settings", lambda: controlled)
    client = redis.Redis.from_url(REDIS_URL, decode_responses=True, socket_timeout=3)
    tenant_id, proxy_id, key_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    external = "synthetic-" + proxy_id.hex
    with database.factory.begin() as session:
        session.add(Tenant(id=tenant_id, company_name="Replay test", region_id="IN1", plan_tier=PlanTier.starter))
        session.flush()
        session.add(ProxyUser(id=proxy_id, tenant_id=tenant_id, external_user_id=external,
            external_user_id_hash=ProxyUserService.hash_external_user_id(str(tenant_id), external)))
        session.add(ApiKey(id=key_id, tenant_id=tenant_id, key_hash="synthetic-unused",
            name="Synthetic", permissions=["write"], is_active=True))
    s = SimpleNamespace(**vars(database), client=client, tenant_id=tenant_id, proxy_id=proxy_id,
                        key_id=key_id, external=external)
    yield s
    # This Redis instance is disposable; limit cleanup to this tenant's keys.
    for key in client.scan_iter():
        if str(tenant_id) in key:
            client.delete(key)
    client.close()


def payload(scope):
    return MemoryAddRequest(external_user_id=scope.external, messages=[
        {"role": "user", "content": "My examples should use C++."}],
        preference_claims=[{"property_key": "examples.language", "value": "C++",
            "applicability": {"kind": "global"}, "turn_index": 0,
            "quote": "My examples should use C++."}])


def args(scope, request=None):
    request = request or payload(scope)
    return dict(requested_user_id=None, authenticated_user_id=None,
        tenant_id=str(scope.tenant_id), external_user_id=request.external_user_id,
        proxy_user_id=str(scope.proxy_id), api_key_id=str(scope.key_id),
        agent_id=request.agent_id, messages=[m.model_dump(mode="json") for m in request.messages],
        metadata=request.metadata, source=None, evidence_mode=request.evidence_mode,
        conversation_id=request.conversation_id,
        preference_claims=[c.model_dump(mode="json", exclude_none=True) for c in request.preference_claims],
        idempotency_key="synthetic-request")


def service(scope, session):
    return MemoryService(session=SessionAdapter(session),
        cache_service=CacheService(client=RedisAdapter(scope.client), use_direct_breaker=True),
        qdrant_service=Forbidden(), embedding_service=Forbidden(),
        quota_manager=SimpleNamespace(get_quota_envelope=AsyncMock(return_value=SimpleNamespace(
            mode=SimpleNamespace(value="FULL")))), dispatch_task=None)


def admit(scope, request=None):
    with scope.factory() as session:
        return run(service(scope, session).queue_memory_add(**args(scope, request)))


def counts(scope):
    with scope.factory() as session:
        return {model.__tablename__: session.scalar(select(func.count()).select_from(model))
                for model in (ExtractionJob, MemorySourceEvent, Memory, MemoryClaimRevision, AuditLog, VectorSyncOutbox)}


def test_five_way_sql_admission_and_queue_accounting(scope):
    barrier = Barrier(5)

    def attempt(_):
        barrier.wait(timeout=10)
        return admit(scope)

    before = counts(scope)
    with ThreadPoolExecutor(max_workers=5) as pool:
        results = list(pool.map(attempt, range(5)))
    assert len({r["job_id"] for r in results}) == 1
    after = counts(scope)
    assert after["extraction_jobs"] - before["extraction_jobs"] == 1
    assert after["memory_source_events"] - before["memory_source_events"] == 1
    assert scope.client.get(_tenant_queue_depth_key(str(scope.tenant_id), "starter-extraction")) == "1"
    members = scope.client.zrange(_queue_jobs_key("starter-extraction"), 0, -1)
    assert len([m for m in members if m.startswith(str(scope.tenant_id) + ":")]) == 1


def test_sql_unique_losers_verify_winner_and_release_reservations(scope, monkeypatch):
    barrier = Barrier(5)
    original_execute = SessionAdapter.execute
    original_create = MemoryService._create_extraction_job

    async def skip_advisory(self, statement, *args, **kwargs):
        if "pg_advisory_xact_lock" in str(statement):
            return None  # Test only: exercise the SQL uniqueness backstop.
        return await original_execute(self, statement, *args, **kwargs)

    async def simultaneous_insert(self, job):
        barrier.wait(timeout=10)
        return await original_create(self, job)

    monkeypatch.setattr(SessionAdapter, "execute", skip_advisory)
    monkeypatch.setattr(MemoryService, "_create_extraction_job", simultaneous_insert)
    before = counts(scope)
    with ThreadPoolExecutor(max_workers=5) as pool:
        results = list(pool.map(lambda _: admit(scope), range(5)))
    assert len({r["job_id"] for r in results}) == 1
    after = counts(scope)
    assert after["extraction_jobs"] - before["extraction_jobs"] == 1
    assert after["memory_source_events"] - before["memory_source_events"] == 1
    assert scope.client.get(_tenant_queue_depth_key(str(scope.tenant_id), "starter-extraction")) == "1"
    members = scope.client.zrange(_queue_jobs_key("starter-extraction"), 0, -1)
    assert len([m for m in members if m.startswith(str(scope.tenant_id) + ":")]) == 1


def test_cache_loss_expiry_and_outage_use_sql_receipt(scope):
    original = admit(scope)
    with scope.factory() as session:
        svc = service(scope, session)
        cache_key = svc.cache_service._idempotency_key("synthetic-request",
            scope=f"tenant:{scope.tenant_id}", operation="memory_add")
    scope.client.delete(cache_key)
    assert admit(scope)["job_id"] == original["job_id"]
    scope.client.set(cache_key, json.dumps({"job_id": "incorrect-stale-cache"}))
    assert admit(scope)["job_id"] == original["job_id"]
    scope.client.pexpire(cache_key, 1)
    time.sleep(0.02)
    assert not scope.client.exists(cache_key)
    assert admit(scope)["job_id"] == original["job_id"]
    # Simulate an expired Redis entry and prohibit even an attempted cache access.
    with scope.factory() as session:
        svc = service(scope, session)
        svc.cache_service = Forbidden()
        svc.queue_router = Forbidden()
        assert run(svc.queue_memory_add(**args(scope)))["job_id"] == original["job_id"]


def test_router_replay_rechecks_revoked_key_despite_stale_request_auth(scope):
    original = admit(scope)
    request = Request({"type": "http", "headers": [], "state": {
        "request_id": "synthetic-router", "api_key_id": str(scope.key_id),
        "api_key_permissions": ["write"], "auth_scheme": "apikey"}})

    def replay():
        with scope.factory() as session:
            return run(memory_routes.add_memories(request=request, response=Response(),
                payload=payload(scope), memory_service=service(scope, session),
                proxy_user_service=Forbidden(), quality_gate_service=Forbidden(),
                tenant_id=str(scope.tenant_id), idempotency_key="synthetic-request"))

    assert replay().job_id == original["job_id"]
    with scope.factory.begin() as session:
        session.get(ApiKey, scope.key_id).is_active = False
    with pytest.raises(APIError) as denied:
        replay()
    assert denied.value.status_code == 403 and denied.value.details is None


def test_full_queue_does_not_commit_receipt_and_same_key_can_retry(scope):
    before = counts(scope)
    depth = _tenant_queue_depth_key(str(scope.tenant_id), "starter-extraction")
    scope.client.set(depth, "50")
    assert admit(scope)["status"] == "queue_full"
    assert counts(scope) == before
    scope.client.delete(depth)
    admitted = admit(scope)
    assert admitted["status"] == "queued"
    assert counts(scope)["extraction_jobs"] - before["extraction_jobs"] == 1
    assert scope.client.get(depth) == "1"


@pytest.mark.parametrize("change", ["owner", "payload"])
def test_changed_request_conflicts_without_new_admission(scope, change):
    original = admit(scope)
    altered = payload(scope).model_dump(mode="json")
    if change == "owner":
        altered["external_user_id"] = "other-synthetic-owner"
    else:
        altered["messages"][0]["content"] = "My examples should use Python."
        altered["preference_claims"][0].update(value="Python", quote="My examples should use Python.")
    before = counts(scope)
    with pytest.raises(APIError) as denied:
        admit(scope, MemoryAddRequest.model_validate(altered))
    assert denied.value.status_code == 409
    assert denied.value.details is None and original["job_id"] not in str(denied.value)
    assert counts(scope) == before


@pytest.mark.parametrize("revocation", ["key", "scope", "user", "tenant"])
def test_current_sql_permissions_override_stale_auth_and_owner_caches(scope, revocation):
    admit(scope)
    with scope.factory.begin() as session:
        if revocation in {"key", "scope"}:
            row = session.get(ApiKey, scope.key_id)
            if revocation == "key":
                row.is_active = False
            else:
                row.permissions = ["read"]
        elif revocation == "user":
            session.get(ProxyUser, scope.proxy_id).is_blocked = True
        else:
            session.get(Tenant, scope.tenant_id).is_active = False
    with pytest.raises(APIError) as denied:
        admit(scope)
    assert denied.value.status_code == 403


@pytest.mark.parametrize("state", [ExtractionJobStatus.queued, ExtractionJobStatus.completed])
def test_redaction_preserves_receipt_beyond_24h_and_while_nonterminal(scope, monkeypatch, state):
    original = admit(scope)
    with scope.factory.begin() as session:
        row = session.get(ExtractionJob, uuid.UUID(original["job_id"]))
        row.created_at = datetime.now(UTC) - timedelta(days=2)
        row.status = state
        row.raw_payload_expires_at = datetime.now(UTC) - timedelta(seconds=1)
        binding = row.request_binding
    monkeypatch.setattr(provenance_tasks, "build_sync_session_factory", lambda: scope.factory)
    provenance_tasks.redact_expired_extraction_payloads.run()
    with scope.factory() as session:
        row = session.get(ExtractionJob, uuid.UUID(original["job_id"]))
        assert row.request_binding == binding and row.request_key_sha256
        assert row.payload_redacted_at and "messages" not in row.payload
        assert row.status == state
    assert admit(scope)["job_id"] == original["job_id"]


@pytest.mark.parametrize("deletion", ["receipt", "user", "tenant"])
def test_deletion_ends_protection_and_stale_redis_is_not_a_receipt(scope, deletion):
    original = admit(scope)
    with scope.factory.begin() as session:
        if deletion == "receipt":
            session.execute(delete(ExtractionJob).where(ExtractionJob.id == uuid.UUID(original["job_id"])))
        elif deletion == "user":
            session.execute(delete(ProxyUser).where(ProxyUser.id == scope.proxy_id))
        else:
            session.execute(delete(Tenant).where(Tenant.id == scope.tenant_id))
    with scope.factory() as session:
        assert session.get(ExtractionJob, uuid.UUID(original["job_id"])) is None
    if deletion == "tenant":
        with pytest.raises(APIError) as denied:
            admit(scope)
        assert denied.value.status_code == 403
        return
    if deletion == "user":
        with scope.factory.begin() as session:
            scope.proxy_id = uuid.uuid4()
            session.add(ProxyUser(id=scope.proxy_id, tenant_id=scope.tenant_id,
                external_user_id=scope.external, external_user_id_hash=
                ProxyUserService.hash_external_user_id(str(scope.tenant_id), scope.external)))
    assert admit(scope)["job_id"] != original["job_id"]


def test_migration_round_trip_and_sql_uniqueness(scope, monkeypatch):
    migration = importlib.import_module("api.db.migrations.versions.durable_add_replay")
    with scope.engine.begin() as connection:
        monkeypatch.setattr(migration, "op", Operations(MigrationContext.configure(connection)))
        migration.downgrade()
        migration.upgrade()
    original = admit(scope)
    with scope.factory() as session:
        row = session.get(ExtractionJob, uuid.UUID(original["job_id"]))
        duplicate = ExtractionJob(id=uuid.uuid4(), tenant_id=row.tenant_id,
            proxy_user_id=row.proxy_user_id, external_user_id=row.external_user_id,
            request_key_sha256=row.request_key_sha256, request_binding=row.request_binding)
        session.add(duplicate)
        with pytest.raises(IntegrityError):
            session.commit()
        session.rollback()


def test_duplicate_worker_delivery_does_not_duplicate_persisted_mutation(scope, monkeypatch):
    original = admit(scope)
    monkeypatch.setattr(extraction_tasks, "build_extraction_session_factory", lambda: scope.factory)
    monkeypatch.setattr(extraction_tasks, "_run_runtime_preference_job", lambda _: None)
    monkeypatch.setattr(extraction_tasks, "_set_job_status", lambda *args: None)
    monkeypatch.setattr(extraction_tasks, "_invalidate_proxy_user_cache", lambda _: None)

    class ControlledEmbedding:
        def __init__(self, **kwargs):
            pass

        def embed_sync(self, text, **kwargs):
            return EmbeddingResult([0.01] * 1536, DEFAULT_ACTIVE_MODEL_ID, 1536, "memories")

    monkeypatch.setattr(extraction_tasks, "EmbeddingService", ControlledEmbedding)
    real_pipeline = extraction_tasks.run_extraction_pipeline
    started, release = Event(), Event()
    def pipeline(job):
        started.set()
        assert release.wait(timeout=10)
        return real_pipeline(job, session_factory=scope.factory, extractor=SimpleNamespace(
            proposal_confirmation_enabled=False), scorer=Forbidden(), qdrant_service=Forbidden())
    monkeypatch.setattr(extraction_tasks, "run_extraction_pipeline", pipeline)
    reference = {"job_id": original["job_id"], "tenant_id": str(scope.tenant_id),
                 "_payload_reference": "extraction_job"}
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(extraction_tasks._process_extraction_job, reference,
                             celery_task_id=original["job_id"])
        assert started.wait(timeout=10)
        try:
            duplicate_processing = extraction_tasks._process_extraction_job(reference,
                celery_task_id="other-synthetic-delivery")
            assert duplicate_processing["status"] == "duplicate_ignored"
        finally:
            release.set()
        first = future.result(timeout=10)
    assert first["memories_created"] == 1
    before = counts(scope)
    duplicate = extraction_tasks._process_extraction_job(reference, celery_task_id=original["job_id"])
    assert duplicate["status"] == "duplicate_ignored"
    assert counts(scope) == before
    with scope.factory() as session:
        assert session.get(ExtractionJob, uuid.UUID(original["job_id"])).status == ExtractionJobStatus.completed
        rows = session.scalars(select(Memory).where(Memory.proxy_user_id == scope.proxy_id)).all()
        assert len(rows) == 1 and not rows[0].is_archived
        assert session.scalar(select(func.count()).select_from(MemoryClaim).where(
            MemoryClaim.proxy_user_id == scope.proxy_id)) == 1
