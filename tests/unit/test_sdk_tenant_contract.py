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


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.asyncio
async def test_sdk_preserves_source_review_and_nonterminal_restatement(asynchronous):
    import json
    review = {
        "id": "review-1", "version": "a" * 64, "kind": "restate_source",
        "question": "What should be remembered?", "actions": ["restate", "dismiss"],
        "expires_at": "2026-10-09T00:00:00Z",
    }
    captured = []
    def handler(request):
        if request.url.path.endswith("retrieve"):
                return httpx.Response(200, json={"data": [], "cached": False,
                    "system_prompt_addition": "", "source_reviews": [review],
                    "request_id": "test", "timestamp": "2026-10-02T00:00:00Z"})
        captured.append(json.loads(request.content))
        return httpx.Response(200, json={"data": {"review_id": "review-1",
            "resolved": False, "action": "restate", "next_step": "add_memory"},
            "request_id": "test", "timestamp": "2026-10-02T00:00:00Z"})
    client = AsyncMemory("test") if asynchronous else Memory("test")
    if asynchronous:
        await client._client.aclose()
        client._client = httpx.AsyncClient(base_url=client.base_url, transport=httpx.MockTransport(handler))
        try:
            retrieved = await client.get(query="language", external_user_id="u1")
            answer = await client.answer_source_review("review-1", external_user_id="u1", version=review["version"], action="restate")
        finally:
            await client.close()
    else:
        client._client.close()
        client._client = httpx.Client(base_url=client.base_url, transport=httpx.MockTransport(handler))
        try:
            retrieved = client.get(query="language", external_user_id="u1")
            answer = client.answer_source_review("review-1", external_user_id="u1", version=review["version"], action="restate")
        finally:
            client.close()
    assert retrieved.source_reviews[0].version == review["version"]
    assert not answer.resolved and answer.next_step == "add_memory"
    assert captured == [{"external_user_id": "u1", "version": review["version"], "action": "restate"}]

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
    "clarification": {
        "id": "clarification-123",
        "conflict_id": "conflict-123",
        "question": "Which plan should be current?",
        "options": [
            {"answer": "A", "label": "Starter", "memory_id": "memory-a"},
            {"answer": "B", "label": "Scale", "memory_id": "memory-b"},
            {"answer": "both", "label": "Both are still correct", "memory_id": None},
            {"answer": "neither", "label": "Neither is correct", "memory_id": None},
        ],
        "expires_at": "2026-08-30T00:00:00Z",
    },
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
        result = client.add(
            messages=[{"role": "user", "content": "I prefer concise answers."}],
            external_user_id="customer-123",
            idempotency_key="event-123",
        )
    finally:
        client.close()

    assert captured["header"] == "event-123"
    assert "idempotency_key" not in str(captured["body"])
    assert result.was_queued is True
    assert result.was_stored is True


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
    assert result.clarification is not None
    assert result.clarification.id == "clarification-123"
    assert result.clarification.options[1].answer == "B"
    assert result.clarification.options[1].memory_id == "memory-b"


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
    statuses = ["failed", "processing", "completed"]

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        assert request.method == "GET"
        assert request.url.path == "/v1/memories/jobs/job/123"
        status = statuses[calls - 1]
        completed = status == "completed"
        return httpx.Response(
            200,
            json={
                "request_id": "request-job-sync",
                "timestamp": "2026-08-30T00:00:00Z",
                "data": {
                    "job_id": "job/123",
                    "status": status,
                    "memories_created": 1 if completed else 0,
                    "created_memory_ids": ["memory-123"] if completed else [],
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
    assert job.created_memory_ids == ["memory-123"]
    assert calls == 3


def test_sync_sdk_stops_waiting_when_job_is_dead() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "request_id": "request-job-dead-sync",
                "timestamp": "2026-08-30T00:00:00Z",
                "data": {"job_id": "job-dead", "status": "dead", "attempts": 3},
            },
        )

    client = Memory("mem_test", base_url="https://api.memoryo.dev")
    client._client.close()
    client._client = httpx.Client(base_url=client.base_url, transport=httpx.MockTransport(handler))
    try:
        job = client.wait_for_job("job-dead", timeout=1, poll_interval=0.001)
    finally:
        client.close()

    assert job.status == "dead"
    assert job.succeeded is False


def test_sync_sdk_answers_clarification_in_customer_chat() -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["path"] = request.url.raw_path.decode()
        captured["body"] = request.read().decode()
        return httpx.Response(
            200,
            json={
                "request_id": "request-clarification-sync",
                "timestamp": "2026-08-30T00:00:00Z",
                "data": {
                    "resolved": True,
                    "clarification_id": "clarification/123",
                    "conflict_id": "conflict-123",
                    "resolution": "B",
                },
            },
        )

    client = Memory("mem_test", base_url="https://api.memoryo.dev")
    client._client.close()
    client._client = httpx.Client(base_url=client.base_url, transport=httpx.MockTransport(handler))
    try:
        result = client.answer_clarification(
            "clarification/123",
            external_user_id="customer-123",
            answer="B",
            free_text="The Scale plan is current.",
        )
    finally:
        client.close()

    assert captured["path"] == "/v1/memories/clarifications/clarification%2F123/answer"
    assert '"external_user_id":"customer-123"' in str(captured["body"])
    assert '"answer":"B"' in str(captured["body"])
    assert result.resolved is True
    assert result.resolution == "B"


@pytest.mark.asyncio
async def test_async_sdk_answers_clarification_in_customer_chat() -> None:
    captured: dict[str, object] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        captured["path"] = request.url.raw_path.decode()
        captured["body"] = (await request.aread()).decode()
        return httpx.Response(
            200,
            json={
                "request_id": "request-clarification-async",
                "timestamp": "2026-08-30T00:00:00Z",
                "data": {
                    "resolved": True,
                    "clarification_id": "clarification-async",
                    "conflict_id": None,
                    "resolution": "neither",
                },
            },
        )

    client = AsyncMemory("mem_test", base_url="https://api.memoryo.dev")
    await client._client.aclose()
    client._client = httpx.AsyncClient(
        base_url=client.base_url,
        transport=httpx.MockTransport(handler),
    )
    try:
        result = await client.answer_clarification(
            "clarification-async",
            external_user_id="customer-async",
            answer="neither",
        )
    finally:
        await client.close()

    assert captured["path"] == "/v1/memories/clarifications/clarification-async/answer"
    assert '"external_user_id":"customer-async"' in str(captured["body"])
    assert result.resolution == "neither"


@pytest.mark.asyncio
async def test_async_sdk_waits_for_memory_job_completion() -> None:
    calls = 0
    statuses = ["failed", "processing", "completed"]

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        status = statuses[calls - 1]
        return httpx.Response(
            200,
            json={
                "request_id": "request-job-async",
                "timestamp": "2026-08-30T00:00:00Z",
                "data": {"job_id": "job-async", "status": status},
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
    assert calls == 3


@pytest.mark.asyncio
async def test_async_sdk_stops_waiting_when_job_is_dead() -> None:
    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "request_id": "request-job-dead-async",
                "timestamp": "2026-08-30T00:00:00Z",
                "data": {"job_id": "job-dead-async", "status": "dead", "attempts": 3},
            },
        )

    client = AsyncMemory("mem_test", base_url="https://api.memoryo.dev")
    await client._client.aclose()
    client._client = httpx.AsyncClient(base_url=client.base_url, transport=httpx.MockTransport(handler))
    try:
        job = await client.wait_for_job("job-dead-async", timeout=1, poll_interval=0.001)
    finally:
        await client.close()

    assert job.status == "dead"
    assert job.succeeded is False


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
