from __future__ import annotations
import uuid
from types import SimpleNamespace

from fastapi.testclient import TestClient

from api import dependencies
from api.main import create_app
from api.middleware.auth import AuthMiddleware
from api.services.quality_gate import GateResult
from api.services.proxy_user_service import ProxyUserBlockedError


class RecordingMemoryService:
    def __init__(self) -> None:
        self.queue_calls = []
        self.next_result = {"job_id": "job_1", "status": "queued"}

    async def queue_memory_add(self, **kwargs):
        self.queue_calls.append(kwargs)
        if self.next_result.get("job_id") is None:
            return dict(self.next_result)
        result = dict(self.next_result)
        if result["job_id"] == "job_1":
            result["job_id"] = f"job_{len(self.queue_calls)}"
        return result


class StubQualityGateService:
    def __init__(self, result: GateResult) -> None:
        self.result = result
        self.calls = []

    async def check(
        self,
        messages,
        tenant_id,
        external_user_id,
        *,
        semantic_deduplication=True,
    ):
        self.calls.append(
            {
                "messages": messages,
                "tenant_id": tenant_id,
                "external_user_id": external_user_id,
                "semantic_deduplication": semantic_deduplication,
            }
        )
        return self.result


class SequencedQualityGateService:
    def __init__(self, results: list[GateResult]) -> None:
        self.results = list(results)
        self.calls = []

    async def check(
        self,
        messages,
        tenant_id,
        external_user_id,
        *,
        semantic_deduplication=True,
    ):
        self.calls.append(
            {
                "messages": messages,
                "tenant_id": tenant_id,
                "external_user_id": external_user_id,
                "semantic_deduplication": semantic_deduplication,
            }
        )
        index = min(len(self.calls) - 1, len(self.results) - 1)
        return self.results[index]


class StubProxyUserService:
    def __init__(self) -> None:
        self.calls = []

    async def resolve(self, tenant_id: str, external_user_id: str, metadata=None):
        self.calls.append(
            {
                "tenant_id": tenant_id,
                "external_user_id": external_user_id,
                "metadata": metadata or {},
            }
        )
        return SimpleNamespace(id=uuid.uuid4())


class BlockedProxyUserService:
    async def resolve(self, tenant_id: str, external_user_id: str, metadata=None):
        raise ProxyUserBlockedError(
            tenant_id=tenant_id,
            external_user_id_hash="blocked-hash",
        )


class FakeRateLimitRedis:
    def __init__(self, *, initial_count: int = 0) -> None:
        self.initial_count = initial_count
        self.counts: dict[str, int] = {}

    async def incr(self, key: str) -> int:
        current = self.counts.get(key, self.initial_count) + 1
        self.counts[key] = current
        return current

    async def ttl(self, key: str) -> int:
        return -1

    async def expire(self, key: str, ttl: int) -> bool:
        return True


async def bypass_api_key_auth(self, request, call_next):
    request.state.tenant_id = str(uuid.uuid4())
    request.state.user_id = None
    request.state.api_key_id = str(uuid.uuid4())
    request.state.auth_scheme = "apikey"
    return await call_next(request)


def build_client(
    monkeypatch,
    *,
    gate_result: GateResult,
    proxy_user_service=None,
    cache_service=None,
    quality_gate_service=None,
) -> tuple[TestClient, RecordingMemoryService, StubQualityGateService]:
    monkeypatch.setattr(AuthMiddleware, "dispatch", bypass_api_key_auth)
    app = create_app()
    app.state.qdrant_service = object()
    app.state.cache_service = cache_service or object()

    memory_service = RecordingMemoryService()
    quality_gate_service = quality_gate_service or StubQualityGateService(gate_result)
    proxy_user_service = proxy_user_service or StubProxyUserService()

    app.dependency_overrides[dependencies.get_memory_service] = lambda: memory_service
    app.dependency_overrides[dependencies.get_quality_gate_service] = lambda: quality_gate_service
    app.dependency_overrides[dependencies.get_proxy_user_service] = lambda: proxy_user_service
    client = TestClient(app)
    return client, memory_service, quality_gate_service


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


def test_add_endpoint_returns_l1_block_metadata(monkeypatch) -> None:
    sequence = SequencedQualityGateService(
        [
            GateResult(
                passed=True,
                blocked_layer=None,
                reason=None,
                budget_remaining_pct=0.55,
            )
            for _ in range(10)
        ]
        + [
            GateResult(
                passed=False,
                blocked_layer="L1",
                reason="rate_limit_exceeded",
                retry_after_seconds=45,
                budget_remaining_pct=0.23,
            )
        ]
    )
    client, memory_service, quality_gate_service = build_client(
        monkeypatch,
        gate_result=GateResult(
            passed=True,
            blocked_layer=None,
            reason=None,
            budget_remaining_pct=0.55,
        ),
        quality_gate_service=sequence,
    )
    with client:
        first_ten = [client.post("/v1/memories/add", json=add_payload()) for _ in range(10)]
        response = client.post("/v1/memories/add", json=add_payload())

    assert all(item.status_code == 200 for item in first_ten)
    assert all(item.json()["status"] == "queued" for item in first_ten)
    assert response.status_code == 200
    assert response.json()["status"] == "L1"
    assert response.json()["job_id"] is None
    assert response.json()["blocked_reason"] == "rate_limit_exceeded"
    assert response.json()["retry_after_seconds"] == 45
    assert response.json()["budget_remaining_pct"] == 0.23
    assert len(memory_service.queue_calls) == 10
    assert len(quality_gate_service.calls) == 11


def test_add_endpoint_returns_l2_block_metadata(monkeypatch) -> None:
    client, memory_service, _quality_gate_service = build_client(
        monkeypatch,
        gate_result=GateResult(
            passed=False,
            blocked_layer="L2",
            reason="low_quality",
            budget_remaining_pct=0.81,
        ),
    )
    with client:
        response = client.post(
            "/v1/memories/add",
            json={
                "external_user_id": "external_user_123",
                "messages": [{"role": "user", "content": "hi"}],
                "metadata": {"session_id": "sess_1"},
            },
        )

    assert response.status_code == 200
    assert response.json()["status"] == "L2"
    assert response.json()["blocked_reason"] == "low_quality"
    assert response.json()["retry_after_seconds"] is None
    assert response.json()["budget_remaining_pct"] == 0.81
    assert len(memory_service.queue_calls) == 0


def test_add_endpoint_returns_l3_block_metadata(monkeypatch) -> None:
    sequence = SequencedQualityGateService(
        [
            GateResult(
                passed=True,
                blocked_layer=None,
                reason=None,
                budget_remaining_pct=0.77,
            ),
            GateResult(
                passed=False,
                blocked_layer="L3",
                reason="duplicate_query",
                budget_remaining_pct=0.76,
            ),
        ]
    )
    client, memory_service, _quality_gate_service = build_client(
        monkeypatch,
        gate_result=GateResult(
            passed=True,
            blocked_layer=None,
            reason=None,
            budget_remaining_pct=0.77,
        ),
        quality_gate_service=sequence,
    )
    with client:
        first_response = client.post("/v1/memories/add", json=add_payload())
        response = client.post("/v1/memories/add", json=add_payload())

    assert first_response.status_code == 200
    assert first_response.json()["status"] == "queued"
    assert response.status_code == 200
    assert response.json()["status"] == "L3"
    assert response.json()["blocked_reason"] == "duplicate_query"
    assert response.json()["budget_remaining_pct"] == 0.76
    assert len(memory_service.queue_calls) == 1


def test_add_endpoint_returns_l4_block_metadata(monkeypatch) -> None:
    client, memory_service, _quality_gate_service = build_client(
        monkeypatch,
        gate_result=GateResult(
            passed=False,
            blocked_layer="L4",
            reason="budget_exhausted",
            budget_remaining_pct=0.0,
        ),
    )
    with client:
        response = client.post("/v1/memories/add", json=add_payload())

    assert response.status_code == 200
    assert response.json()["status"] == "L4"
    assert response.json()["blocked_reason"] == "budget_exhausted"
    assert response.json()["budget_remaining_pct"] == 0.0
    assert len(memory_service.queue_calls) == 0


def test_add_endpoint_queues_job_when_gate_passes(monkeypatch) -> None:
    client, memory_service, quality_gate_service = build_client(
        monkeypatch,
        gate_result=GateResult(
            passed=True,
            blocked_layer=None,
            reason=None,
            budget_remaining_pct=0.65,
        ),
    )
    with client:
        response = client.post(
            "/v1/memories/add",
            json={
                "external_user_id": "external_user_123",
                "messages": [
                    {"role": "user", "content": "I am building a B2B AI memory platform."},
                    {"role": "assistant", "content": "What stack are you using?"},
                    {"role": "user", "content": "FastAPI, Redis, Qdrant, and PostgreSQL."},
                    {"role": "assistant", "content": "What problem are you solving first?"},
                    {"role": "user", "content": "Tenant isolation, quota controls, and retrieval quality."},
                ],
                "metadata": {"session_id": "sess_1"},
            },
        )

    assert response.status_code == 200
    assert response.json()["status"] == "queued"
    assert response.json()["job_id"] == "job_1"
    assert response.json()["blocked_reason"] is None
    assert response.json()["retry_after_seconds"] is None
    assert response.json()["budget_remaining_pct"] == 0.65
    assert len(memory_service.queue_calls) == 1
    assert len(quality_gate_service.calls) == 1


def test_add_endpoint_forwards_source_provenance(monkeypatch) -> None:
    client, memory_service, quality_gate_service = build_client(
        monkeypatch,
        gate_result=GateResult(
            passed=True,
            blocked_layer=None,
            reason=None,
            budget_remaining_pct=0.9,
        ),
    )
    payload = add_payload()
    payload["source"] = {
        "event_id": "ticket-event-42",
        "service": "support-service",
        "observed_at": "2026-06-11T10:00:00Z",
        "scope": {"ticket_id": "TCK-42"},
        "evidence": [
            {
                "source_type": "ticket",
                "reference": "TCK-42",
                "content_hash": "a" * 64,
            }
        ],
    }

    with client:
        response = client.post("/v1/memories/add", json=payload)

    assert response.status_code == 200
    queued = memory_service.queue_calls[0]
    assert queued["source"]["event_id"] == "ticket-event-42"
    assert queued["source"]["service"] == "support-service"
    assert queued["source"]["scope"] == {"ticket_id": "TCK-42"}
    assert queued["api_key_id"] is not None
    assert quality_gate_service.calls[0]["semantic_deduplication"] is False


def test_add_endpoint_returns_passthrough_when_memory_service_skips_extraction(monkeypatch) -> None:
    client, memory_service, _quality_gate_service = build_client(
        monkeypatch,
        gate_result=GateResult(
            passed=True,
            blocked_layer=None,
            reason=None,
            budget_remaining_pct=0.22,
        ),
    )
    memory_service.next_result = {
        "job_id": None,
        "status": "passthrough",
        "budget_remaining_pct": 0.22,
    }
    with client:
        response = client.post("/v1/memories/add", json=add_payload())

    assert response.status_code == 200
    assert response.json()["status"] == "passthrough"
    assert response.json()["job_id"] is None
    assert response.json()["budget_remaining_pct"] == 0.22


def test_add_endpoint_returns_403_when_proxy_user_is_blocked_before_quality_gate(monkeypatch) -> None:
    client, memory_service, quality_gate_service = build_client(
        monkeypatch,
        gate_result=GateResult(
            passed=True,
            blocked_layer=None,
            reason=None,
            budget_remaining_pct=0.5,
        ),
        proxy_user_service=BlockedProxyUserService(),
    )

    with client:
        response = client.post("/v1/memories/add", json=add_payload())

    assert response.status_code == 403
    assert response.json()["error"] == "proxy_user_blocked"
    assert len(memory_service.queue_calls) == 0
    assert len(quality_gate_service.calls) == 1


def test_add_endpoint_returns_429_when_tenant_rate_limit_is_exceeded(monkeypatch) -> None:
    monkeypatch.setenv("TENANT_RATE_LIMIT_PER_MINUTE", "2")
    fake_cache_service = SimpleNamespace(client=FakeRateLimitRedis(initial_count=2))
    client, memory_service, quality_gate_service = build_client(
        monkeypatch,
        gate_result=GateResult(
            passed=True,
            blocked_layer=None,
            reason=None,
            budget_remaining_pct=0.5,
        ),
        cache_service=fake_cache_service,
    )

    with client:
        response = client.post("/v1/memories/add", json=add_payload())

    assert response.status_code == 429
    assert response.json()["error"] == "rate_limited"
    assert response.headers["Retry-After"]
    assert len(memory_service.queue_calls) == 0
    assert len(quality_gate_service.calls) == 0
