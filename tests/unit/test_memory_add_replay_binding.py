"""Synthetic cache-boundary regressions; no pilot data or external services."""
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from starlette.requests import Request
from starlette.responses import Response

from api.db.cache import CacheService
from api.errors import APIError
from api.routers.memories import add_memories
from api.schemas.requests import MemoryAddRequest
from api.services.memory_add_replay import REPLAY_BINDING_FIELD, memory_add_replay_binding
from api.services.memory_service import MemoryService
from api.db.models import ExtractionJobStatus
from api.services.proxy_user_service import ProxyUserService

TENANT = "00000000-0000-4000-8000-000000000011"


def run(coroutine):
    """Complete in-memory awaits without Windows socket-based event-loop setup."""
    try:
        coroutine.send(None)
    except StopIteration as result:
        return result.value
    else:
        raise AssertionError("unexpected async suspension; no work scheduled")
    finally:
        coroutine.close()


class RedisMemory:
    def __init__(self):
        self.values = {}
        self.sets = 0

    async def get(self, key):
        return self.values.get(key)

    async def set(self, key, value, *, ex):
        self.values[key] = value
        self.sets += 1


class Forbidden:
    def __getattr__(self, name):
        raise AssertionError(f"unexpected dependency access: {name}")


@pytest.fixture(autouse=True)
def prohibit_network(monkeypatch):
    import socket

    def deny(*args, **kwargs):
        raise AssertionError("network prohibited")

    monkeypatch.setattr(socket.socket, "connect", deny)
    monkeypatch.setattr(socket.socket, "connect_ex", deny)
    monkeypatch.setattr(socket, "getaddrinfo", deny)


def make_service():
    redis = RedisMemory()
    cache = CacheService(client=redis, use_direct_breaker=True)
    service = object.__new__(MemoryService)
    service.cache_service = cache
    service.receipt = None
    service.session = SimpleNamespace(
        get=AsyncMock(return_value=SimpleNamespace(is_blocked=False, tenant_id=TENANT,
            external_user_id_hash=ProxyUserService.hash_external_user_id(TENANT, "synthetic-a"))),
        execute=AsyncMock(side_effect=lambda *args, **kwargs:
            SimpleNamespace(scalar_one_or_none=lambda: service.receipt)),
    )
    # Real current-permission checks are covered by the isolated SQL suite.
    service._check_replay_permissions = AsyncMock()
    service.quota_manager = SimpleNamespace(get_quota_envelope=AsyncMock(
        return_value=SimpleNamespace(mode=SimpleNamespace(value="FULL"))))
    return service, redis


def payload():
    return MemoryAddRequest(external_user_id="synthetic-a", messages=[
        {"role": "user", "content": "Synthetic blue setting."}],
        metadata={"purpose": "synthetic"}, conversation_id="synthetic-session")


def binding_for(request):
    return memory_add_replay_binding(
        tenant_id=TENANT, owner_id=request.external_user_id,
        messages=[m.model_dump(mode="json") for m in request.messages],
        metadata=request.metadata, agent_id=request.agent_id,
        source=request.source.model_dump(mode="json") if request.source else None,
        evidence_mode=request.evidence_mode, conversation_id=request.conversation_id,
        preference_claims=[c.model_dump(mode="json", exclude_none=True)
                           for c in request.preference_claims]
                          if request.preference_claims is not None else None,
    )


def seed(service, entry):
    service.receipt = SimpleNamespace(request_binding=entry.get(REPLAY_BINDING_FIELD),
        proxy_user_id="synthetic-proxy", payload={k: v for k, v in entry.items() if k != REPLAY_BINDING_FIELD},
        id=entry["job_id"], status=ExtractionJobStatus.queued, memories_created=0, result={},
        attempts=0, max_attempts=3, error_type=None, error=None)
    run(service.cache_service.set_idempotent_response(
        "synthetic-key", entry, scope=f"tenant:{TENANT}", operation="memory_add"))


def router_call(service, request):
    return run(add_memories(
        request=Request({"type": "http", "headers": [], "state": {"request_id": "synthetic"}}),
        response=Response(), payload=request, memory_service=service,
        proxy_user_service=Forbidden(), quality_gate_service=Forbidden(),
        tenant_id=TENANT, idempotency_key="synthetic-key"))


def service_call(service, request):
    return run(service.queue_memory_add(
        requested_user_id=None, authenticated_user_id=None, tenant_id=TENANT,
        external_user_id=request.external_user_id, proxy_user_id="synthetic-proxy",
        agent_id=request.agent_id, messages=[m.model_dump(mode="json") for m in request.messages],
        metadata=request.metadata, idempotency_key="synthetic-key",
        source=request.source.model_dump(mode="json") if request.source else None,
        evidence_mode=request.evidence_mode, conversation_id=request.conversation_id))


@pytest.mark.parametrize("boundary", [router_call, service_call])
def test_exact_replay_returns_prior_job_without_internal_binding(boundary):
    service, redis = make_service()
    original = payload()
    seed(service, {"job_id": "prior-job", "status": "queued",
                   REPLAY_BINDING_FIELD: binding_for(original)})
    before = dict(redis.values)
    result = boundary(service, original)
    data = result.model_dump() if hasattr(result, "model_dump") else result
    assert data["job_id"] == "prior-job"
    assert REPLAY_BINDING_FIELD not in data
    assert redis.values == before and redis.sets == 1


@pytest.mark.parametrize("boundary", [router_call, service_call])
@pytest.mark.parametrize("change", ["owner", "message", "metadata", "conversation", "agent", "source", "evidence_mode"])
def test_changed_request_refused_before_new_work(boundary, change):
    service, redis = make_service()
    original = payload()
    seed(service, {"job_id": "prior-job", "status": "queued",
                   REPLAY_BINDING_FIELD: binding_for(original)})
    altered = original.model_dump(mode="json")
    if change == "owner":
        altered["external_user_id"] = "synthetic-b"
    elif change == "message":
        altered["messages"][0]["content"] = "Synthetic green setting."
    elif change == "metadata":
        altered["metadata"]["purpose"] = "changed"
    elif change == "conversation":
        altered["conversation_id"] = "other-session"
    elif change == "agent":
        altered["agent_id"] = "other-agent"
    elif change == "source":
        altered["source"] = {"event_id": "synthetic-event", "service": "synthetic",
                             "observed_at": "2026-10-11T00:00:00Z"}
    else:
        altered["evidence_mode"] = "client_assertion"
    before = dict(redis.values)
    with pytest.raises(APIError) as failure:
        boundary(service, MemoryAddRequest.model_validate(altered))
    assert failure.value.status_code == 409
    assert failure.value.error == "memory_add_replay_mismatch"
    assert failure.value.details is None and "prior-job" not in str(failure.value)
    assert redis.values == before and redis.sets == 1


@pytest.mark.parametrize("boundary", [router_call, service_call])
@pytest.mark.parametrize("binding", [None, "invalid", {"version": 2}, {"version": 1}])
def test_unbound_or_invalid_entry_fails_closed(boundary, binding):
    service, redis = make_service()
    seed(service, {"job_id": "prior-job", "status": "queued", REPLAY_BINDING_FIELD: binding})
    before = dict(redis.values)
    with pytest.raises(APIError) as failure:
        boundary(service, payload())
    assert failure.value.status_code == 409
    assert failure.value.details is None and "prior-job" not in str(failure.value)
    assert redis.values == before and redis.sets == 1


def test_missing_cache_entry_remains_a_miss():
    service, _ = make_service()
    assert run(service.get_idempotent_memory_add(
        tenant_id=TENANT, idempotency_key="synthetic-key",
        replay_binding=binding_for(payload()))) is None


def test_binding_is_canonical_and_covers_preference_and_server_inputs():
    args = dict(tenant_id="synthetic-tenant", owner_id="synthetic-a",
                messages=[{"content": "Synthetic blue."}], metadata={"a": 1, "b": 2},
                preference_claims=[{"property_key": "synthetic.color", "value": "blue",
                                    "applicability": {"kind": "global"},
                                    "replaces_memory_id": "old", "replaces_version": "version-1"}])
    original = memory_add_replay_binding(**args)
    reordered = deepcopy(args)
    reordered["metadata"] = {"b": 2, "a": 1}
    assert memory_add_replay_binding(**reordered) == original
    for field, value in [("value", "green"), ("applicability", {"kind": "context", "key": "other"}),
                         ("replaces_version", "version-2"), ("replaces_memory_id", "other")]:
        changed = deepcopy(args)
        changed["preference_claims"][0][field] = value
        assert memory_add_replay_binding(**changed) != original
    for field, value in [("tenant_id", "other-tenant"), ("api_key_id", "other-key"),
                         ("trusted_submission_kind", "mcp_client_assertion"),
                         ("turn_preparation", {"sha256": "other"})]:
        assert memory_add_replay_binding(**{**args, field: value}) != original


def test_new_acceptance_stores_binding_without_polluting_job_status():
    # Exercise real cache-write placement with controlled admission/dispatch;
    # no tenant provenance or durable database claim is made by this test.
    service, redis = make_service()

    async def admitted(job):
        return job, True

    service._create_extraction_job = AsyncMock(side_effect=admitted)
    service._dispatch_extraction_job = AsyncMock(return_value=None)
    original = run(service.queue_memory_add(
        requested_user_id="synthetic-owner", authenticated_user_id=None,
        agent_id=None, messages=[{"role": "user", "content": "Synthetic blue."}],
        metadata={}, idempotency_key="synthetic-key"))
    cached = run(service.cache_service.get_idempotent_response(
        "synthetic-key", scope="user:synthetic-owner", operation="memory_add"))
    assert cached["job_id"] == original["job_id"]
    assert cached[REPLAY_BINDING_FIELD]["owner_id"] == "synthetic-owner"
    status = run(service.cache_service.get_job_status(original["job_id"]))
    assert REPLAY_BINDING_FIELD not in status and REPLAY_BINDING_FIELD not in original
    replay = run(service.queue_memory_add(
        requested_user_id="synthetic-owner", authenticated_user_id=None,
        agent_id=None, messages=[{"role": "user", "content": "Synthetic blue."}],
        metadata={}, idempotency_key="synthetic-key"))
    assert replay["job_id"] == original["job_id"]
    assert service._create_extraction_job.await_count == 1
    assert service._dispatch_extraction_job.await_count == 1
