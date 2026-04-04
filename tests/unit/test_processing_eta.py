from __future__ import annotations
import json
import uuid
from types import SimpleNamespace

from fastapi.testclient import TestClient

from api import dependencies
from api.celery_app import celery_app
from api.main import create_app
from api.middleware.auth import AuthMiddleware
from api.services.quality_gate import GateResult
from api.tasks import queue_router


class FakeSyncRedis:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}

    def get(self, key: str):
        return self.values.get(key)

    def set(self, key: str, value: str, ex: int | None = None):
        self.values[key] = value
        return True


class FakeInspector:
    def __init__(self, *, active=None, reserved=None, error: Exception | None = None) -> None:
        self._active = active or {}
        self._reserved = reserved or {}
        self._error = error
        self.calls = 0

    def active(self):
        self.calls += 1
        if self._error is not None:
            raise self._error
        return self._active

    def reserved(self):
        if self._error is not None:
            raise self._error
        return self._reserved


class RecordingMemoryService:
    def __init__(self) -> None:
        self.next_result = {"job_id": "job_1", "status": "queued"}

    async def queue_memory_add(self, **kwargs):
        return dict(self.next_result)


class StubQualityGateService:
    def __init__(self, result: GateResult) -> None:
        self.result = result

    async def check(self, messages, tenant_id, external_user_id):
        return self.result


class StubProxyUserService:
    async def resolve(self, tenant_id: str, external_user_id: str, metadata=None):
        return SimpleNamespace(id=uuid.uuid4())


async def bypass_api_key_auth(self, request, call_next):
    request.state.tenant_id = str(uuid.uuid4())
    request.state.user_id = None
    request.state.auth_scheme = "apikey"
    request.state.request_id = "req_processing_eta"
    return await call_next(request)


def build_client(monkeypatch, *, gate_result: GateResult):
    monkeypatch.setattr(AuthMiddleware, "dispatch", bypass_api_key_auth)
    app = create_app()
    app.state.qdrant_service = object()
    app.state.cache_service = object()
    app.dependency_overrides[dependencies.get_memory_service] = lambda: RecordingMemoryService()
    app.dependency_overrides[dependencies.get_quality_gate_service] = lambda: StubQualityGateService(gate_result)
    app.dependency_overrides[dependencies.get_proxy_user_service] = lambda: StubProxyUserService()
    return TestClient(app)


def add_payload() -> dict:
    return {
        "external_user_id": "external_user_123",
        "messages": [
            {"role": "user", "content": "I am building a B2B AI memory platform."},
            {"role": "assistant", "content": "What do you need help with?"},
            {"role": "user", "content": "Quota controls, tenant isolation, and auditability."},
        ],
        "metadata": {"session_id": "sess_1"},
    }


def test_get_queue_depth_counts_active_and_reserved_jobs_and_caches(monkeypatch) -> None:
    redis_client = FakeSyncRedis()
    inspector = FakeInspector(
        active={
            "worker-1": [
                {"delivery_info": {"routing_key": "growth-extraction"}},
                {"delivery_info": {"routing_key": "starter-extraction"}},
            ]
        },
        reserved={
            "worker-2": [
                {"delivery_info": {"routing_key": "growth-extraction"}},
            ]
        },
    )
    monkeypatch.setattr(queue_router, "_redis_sync_client", lambda: redis_client)
    monkeypatch.setattr(
        celery_app.control,
        "inspect",
        lambda: inspector,
    )

    first = queue_router.get_queue_depth("growth-extraction")
    second = queue_router.get_queue_depth("growth-extraction")

    assert first == 2
    assert second == 2
    assert inspector.calls == 1
    assert redis_client.values["queue_depth:growth-extraction"] == "2"


def test_get_queue_depth_returns_zero_on_inspect_error(monkeypatch) -> None:
    redis_client = FakeSyncRedis()
    inspector = FakeInspector(error=RuntimeError("inspect failed"))
    monkeypatch.setattr(queue_router, "_redis_sync_client", lambda: redis_client)
    monkeypatch.setattr(
        celery_app.control,
        "inspect",
        lambda: inspector,
    )

    depth = queue_router.get_queue_depth("starter-extraction")

    assert depth == 0


def test_get_processing_eta_returns_none_below_threshold(monkeypatch) -> None:
    monkeypatch.setattr(queue_router, "get_extraction_queue_sync", lambda **kwargs: "growth-extraction")
    monkeypatch.setattr(queue_router, "get_queue_depth", lambda queue_name: 10)

    eta = queue_router.get_processing_eta(str(uuid.uuid4()))

    assert eta is None


def test_get_processing_eta_returns_seconds_when_delayed(monkeypatch) -> None:
    monkeypatch.setattr(queue_router, "get_extraction_queue_sync", lambda **kwargs: "growth-extraction")
    monkeypatch.setattr(queue_router, "get_queue_depth", lambda queue_name: 30)

    eta = queue_router.get_processing_eta(str(uuid.uuid4()))

    assert eta == 360


def test_add_endpoint_includes_processing_eta_and_header_when_delayed(monkeypatch) -> None:
    monkeypatch.setattr("api.routers.memories.get_processing_eta", lambda tenant_id: 360)
    client = build_client(
        monkeypatch,
        gate_result=GateResult(
            passed=True,
            blocked_layer=None,
            reason=None,
            budget_remaining_pct=0.65,
        ),
    )

    with client:
        response = client.post("/v1/memories/add", json=add_payload())

    assert response.status_code == 200
    assert response.json()["status"] == "queued"
    assert response.json()["processing_eta_seconds"] == 360
    assert response.json()["processing_status"] == "delayed"
    assert response.headers["X-MemoryOS-Processing"] == "delayed"


def test_add_endpoint_returns_normal_processing_when_eta_is_none(monkeypatch) -> None:
    monkeypatch.setattr("api.routers.memories.get_processing_eta", lambda tenant_id: None)
    client = build_client(
        monkeypatch,
        gate_result=GateResult(
            passed=True,
            blocked_layer=None,
            reason=None,
            budget_remaining_pct=0.65,
        ),
    )

    with client:
        response = client.post("/v1/memories/add", json=add_payload())

    assert response.status_code == 200
    assert response.json()["processing_eta_seconds"] is None
    assert response.json()["processing_status"] == "normal"
    assert response.headers["X-MemoryOS-Processing"] == "normal"


def test_add_endpoint_omits_processing_fields_for_blocked_response(monkeypatch) -> None:
    monkeypatch.setattr("api.routers.memories.get_processing_eta", lambda tenant_id: 360)
    client = build_client(
        monkeypatch,
        gate_result=GateResult(
            passed=False,
            blocked_layer="L2",
            reason="low_quality",
            budget_remaining_pct=0.81,
        ),
    )

    with client:
        response = client.post("/v1/memories/add", json=add_payload())

    body = response.json()
    assert response.status_code == 200
    assert body["status"] == "L2"
    assert "processing_eta_seconds" not in body
    assert "processing_status" not in body
    assert "X-MemoryOS-Processing" not in response.headers
