from __future__ import annotations

import sys
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

SDK_PATH = Path(__file__).resolve().parents[2] / "sdk" / "python"
if str(SDK_PATH) not in sys.path:
    sys.path.insert(0, str(SDK_PATH))

from memoryos import AsyncMemory, Memory

ADD_RESPONSE = {
    "job_id": "job-123",
    "status": "queued",
    "request_id": "request-123",
    "timestamp": "2026-08-29T00:00:00Z",
}

RETRIEVE_RESPONSE = {
    "retrieval_id": "retrieval-123",
    "data": [],
    "cached": False,
    "system_prompt_addition": "",
    "clarification_question": "Which plan should be current?",
    "request_id": "request-123",
    "timestamp": "2026-08-29T00:00:00Z",
}


def test_sync_sdk_forwards_idempotency_header_and_not_body() -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["header"] = request.headers.get("Idempotency-Key")
        captured["body"] = request.read().decode()
        return httpx.Response(200, json=ADD_RESPONSE)

    client = Memory("mem_test", base_url="https://api.memoryo.dev")
    client._client.close()
    client._client = httpx.Client(
        base_url=client.base_url,
        transport=httpx.MockTransport(handler),
    )
    try:
        client.add(
            messages=[{"role": "user", "content": "I prefer concise answers."}],
            external_user_id="customer-123",
            idempotency_key="event-123",
        )
    finally:
        client.close()

    assert captured["header"] == "event-123"
    assert "idempotency_key" not in str(captured["body"])


def test_sync_sdk_sends_as_of_and_preserves_clarification() -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = request.read().decode()
        return httpx.Response(200, json=RETRIEVE_RESPONSE)

    client = Memory("mem_test", base_url="https://api.memoryo.dev")
    client._client.close()
    client._client = httpx.Client(
        base_url=client.base_url,
        transport=httpx.MockTransport(handler),
    )
    try:
        result = client.get(
            query="What plan was active?",
            external_user_id="customer-123",
            as_of=datetime(2026, 8, 1, 12, 0, tzinfo=UTC),
        )
    finally:
        client.close()

    assert '"as_of":"2026-08-01T12:00:00+00:00"' in str(captured["body"])
    assert result.clarification_question == "Which plan should be current?"


@pytest.mark.asyncio
async def test_async_sdk_forwards_idempotency_header() -> None:
    captured: dict[str, object] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        captured["header"] = request.headers.get("Idempotency-Key")
        captured["body"] = (await request.aread()).decode()
        return httpx.Response(200, json=ADD_RESPONSE)

    client = AsyncMemory("mem_test", base_url="https://api.memoryo.dev")
    await client._client.aclose()
    client._client = httpx.AsyncClient(
        base_url=client.base_url,
        transport=httpx.MockTransport(handler),
    )
    try:
        await client.add(
            messages=[{"role": "user", "content": "I prefer concise answers."}],
            external_user_id="customer-123",
            idempotency_key="event-async-123",
        )
    finally:
        await client.close()

    assert captured["header"] == "event-async-123"
    assert "idempotency_key" not in str(captured["body"])


def test_sdk_defaults_use_canonical_hosted_api() -> None:
    assert Memory.DEFAULT_BASE_URL == "https://api.memoryo.dev"
    assert AsyncMemory.DEFAULT_BASE_URL == "https://api.memoryo.dev"


def test_sync_sdk_waits_for_memory_job_completion() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        assert request.method == "GET"
        assert request.url.path == "/v1/memories/jobs/job/123"
        completed = calls > 1
        return httpx.Response(
            200,
            json={
                "request_id": "request-job-sync",
                "timestamp": "2026-08-30T00:00:00Z",
                "data": {
                    "job_id": "job/123",
                    "status": "completed" if completed else "processing",
                    "memories_created": 1 if completed else 0,
                    "attempts": 1,
                }
            },
        )

    client = Memory("mem_test", base_url="https://api.memoryo.dev")
    client._client.close()
    client._client = httpx.Client(base_url=client.base_url, transport=httpx.MockTransport(handler))
    try:
        job = client.wait_for_job("job/123", timeout=1, poll_interval=0.001)
    finally:
        client.close()

    assert job.succeeded is True
    assert job.memories_created == 1


@pytest.mark.asyncio
async def test_async_sdk_waits_for_memory_job_completion() -> None:
    calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        completed = calls > 1
        return httpx.Response(
            200,
            json={
                "request_id": "request-job-async",
                "timestamp": "2026-08-30T00:00:00Z",
                "data": {"job_id": "job-async", "status": "completed" if completed else "queued"},
            },
        )

    client = AsyncMemory("mem_test", base_url="https://api.memoryo.dev")
    await client._client.aclose()
    client._client = httpx.AsyncClient(base_url=client.base_url, transport=httpx.MockTransport(handler))
    try:
        job = await client.wait_for_job("job-async", timeout=1, poll_interval=0.001)
    finally:
        await client.close()

    assert job.succeeded is True


def test_sync_list_scopes_request_to_external_user() -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["params"] = dict(request.url.params)
        return httpx.Response(
            200,
            json={
                "data": [],
                "pagination": {"next_cursor": None, "limit": 50, "total": 0},
                "request_id": "request-123",
                "timestamp": "2026-08-29T00:00:00Z",
            },
        )

    client = Memory("mem_test", base_url="https://api.memoryo.dev")
    client._client.close()
    client._client = httpx.Client(base_url=client.base_url, transport=httpx.MockTransport(handler))
    try:
        client.list(external_user_id="customer-123")
    finally:
        client.close()

    assert captured["params"] == {"external_user_id": "customer-123", "limit": "50"}


def test_sync_export_uses_tenant_proxy_user_route_and_schema() -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["path"] = request.url.raw_path.decode()
        return httpx.Response(
            200,
            json={
                "data": {
                    "tenant_id": "tenant-123",
                    "proxy_user_id": "proxy-123",
                    "memories": [],
                },
                "request_id": "request-123",
                "timestamp": "2026-08-29T00:00:00Z",
            },
        )

    client = Memory("mem_test", base_url="https://api.memoryo.dev")
    client._client.close()
    client._client = httpx.Client(base_url=client.base_url, transport=httpx.MockTransport(handler))
    try:
        result = client.export(external_user_id="customer/123")
    finally:
        client.close()

    assert captured["path"] == "/v1/users/customer%2F123/export"
    assert result.tenant_id == "tenant-123"
    assert result.proxy_user_id == "proxy-123"


def test_sync_delete_does_not_require_external_user_id() -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        return httpx.Response(
            200,
            json={
                "data": {"deleted": True},
                "request_id": "request-123",
                "timestamp": "2026-08-29T00:00:00Z",
            },
        )

    client = Memory("mem_test", base_url="https://api.memoryo.dev")
    client._client.close()
    client._client = httpx.Client(base_url=client.base_url, transport=httpx.MockTransport(handler))
    try:
        deleted = client.delete("memory-123")
    finally:
        client.close()

    assert deleted is True
    assert captured["url"] == "https://api.memoryo.dev/v1/memories/memory-123?hard_delete=false"
