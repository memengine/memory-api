"""Standalone SDK serialization and backend contract parity, no provider calls."""
from __future__ import annotations

import inspect
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
import pytest_asyncio

SDK_PATH = Path(__file__).resolve().parents[2] / "sdk" / "python"
if str(SDK_PATH) not in sys.path:
    sys.path.insert(0, str(SDK_PATH))

from memoryos import (
    AsyncMemory,
    Memory,
    MemoryOSError,
    PreferenceClaimRequest,
    PreferenceReadContext,
)
from memoryos.preferences import PreferenceApplicability
from memoryos.types import AddRequest, MemoryJobStatus, MemoryRecord, MemoryResult

from api.schemas import preferences as backend
from api.schemas.requests import MemoryAddRequest, MemoryRetrieveRequest

SCOPE = {"kind": "context", "namespace": "project", "key": "release-check"}
CLAIM = {
    "property_key": "examples.language", "value": "Python", "applicability": SCOPE,
    "turn_index": 0, "quote": "For Release Check, use Python for examples.",
    "commitment": "replacement",
    "replaces_memory_id": "00000000-0000-4000-8000-000000000001",
    "replaces_version": "a" * 64,
}
CONTEXT = {"applicability": SCOPE, "property_keys": ["examples.language"]}


@pytest.fixture(params=[False, True], ids=["sync", "async"])
def asynchronous(request):
    return request.param


async def invoke(client, method, *args, **kwargs):
    result = getattr(client, method)(*args, **kwargs)
    return await result if inspect.isawaitable(result) else result


@pytest_asyncio.fixture
async def sdk(asynchronous):
    captured = []

    def handler(request):
        body = json.loads(request.content) if request.content else None
        captured.append((request, body))
        if request.url.path.endswith("/add"):
            # The actual backend schema/evidence validator accepts the SDK wire payload.
            MemoryAddRequest.model_validate(body)
            return httpx.Response(200, json={"status": "queued", "job_id": "j",
                "request_id": "r", "timestamp": "2026-10-03T00:00:00Z"})
        if request.url.path.endswith("/retrieve"):
            MemoryRetrieveRequest.model_validate(body)
            return httpx.Response(200, json={"data": [{"id": "m", "content": "Python",
                "category": "preference", "importance_score": 5, "relevance_score": 1,
                "context_snippet": "Python", "provenance": {"preference_version": "b" * 64,
                    "unknown_future_field": {"keep": True}}}],
                "cached": False, "system_prompt_addition": "Python",
                "request_id": "r", "timestamp": "2026-10-03T00:00:00Z"})
        return httpx.Response(200, json={"data": {"job_id": "j", "status": "completed",
            "created_memory_ids": [], "extraction_metadata": {"source_reviews_capacity_rejected": 2,
                "source_reviews_closed_replays": 1, "future_field": "preserved"}},
            "request_id": "r", "timestamp": "2026-10-03T00:00:00Z"})

    client = AsyncMemory("test") if asynchronous else Memory("test")
    await invoke(client._client, "aclose") if asynchronous else client._client.close()
    cls = httpx.AsyncClient if asynchronous else httpx.Client
    client._client = cls(base_url=client.base_url, transport=httpx.MockTransport(handler))
    try:
        yield client, captured
    finally:
        await invoke(client, "close")


@pytest.mark.asyncio
@pytest.mark.parametrize("typed", [False, True])
async def test_preference_write_read_and_outcomes(sdk, typed):
    client, captured = sdk
    claim = PreferenceClaimRequest(**CLAIM) if typed else CLAIM
    write = await invoke(client, "add", [{"role": "user", "content": CLAIM["quote"]}], "user-1",
        preference_claims=[claim], idempotency_key="evt-1", conversation_id="chat-1")
    assert write.was_queued and write.job_id == "j"
    assert captured[-1][1]["preference_claims"] == [CLAIM]
    assert captured[-1][1]["conversation_id"] == "chat-1"
    assert captured[-1][0].headers["Idempotency-Key"] == "evt-1"
    context = PreferenceReadContext(**CONTEXT) if typed else CONTEXT
    result = await invoke(client, "get", "Which language?", "user-1", preference_context=context)
    assert captured[-1][1]["preference_context"] == CONTEXT
    assert result.items[0].provenance == {
        "preference_version": "b" * 64, "unknown_future_field": {"keep": True},
    }
    job = await invoke(client, "get_job_status", "j")
    assert job.succeeded and not job.created_memory_ids
    assert job.source_reviews_capacity_rejected == 2
    assert job.extraction_metadata["future_field"] == "preserved"
    assert len(captured) == 3  # No implicit poll, provider call, or extra request.


@pytest.mark.asyncio
async def test_old_calls_omit_optional_fields_and_do_not_poll(sdk):
    client, captured = sdk
    await invoke(client, "add", [{"role": "user", "content": "Hello"}], "user-1",
        None, None, "evt-1", None, "chat-1")
    await invoke(client, "get", "language", "user-1", 10, None, None, None, "bullets", 500, None)
    assert "preference_claims" not in captured[0][1]
    assert "preference_context" not in captured[1][1]
    assert captured[0][0].headers["Idempotency-Key"] == "evt-1"
    assert len(captured) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("commitment,applicability", [
    ("asserted", {"kind": "global"}),
    ("uncertain", SCOPE),
    ("asserted", {"kind": "unresolved"}),
])
async def test_sdk_preserves_declared_commitment_without_inference(sdk, commitment, applicability):
    client, captured = sdk
    claim = {key: value for key, value in CLAIM.items() if not key.startswith("replaces_")}
    claim.update(commitment=commitment, applicability=applicability)
    await invoke(client, "add", [{"role": "user", "content": claim["quote"]}], "u",
        preference_claims=[claim])
    sent = captured[0][1]["preference_claims"][0]
    assert sent.get("commitment", "asserted") == commitment
    assert sent["applicability"] == applicability
    assert sent["value"] == "Python" and "replaces_memory_id" not in sent
    assert len(captured) == 1


@pytest.mark.asyncio
async def test_unsupported_combinations_fail_without_network(sdk):
    client, captured = sdk
    for options in ({"agent_id": "a"}, {"source": {"event_id": "e", "service": "billing",
            "observed_at": datetime.now(UTC)}}):
        with pytest.raises(ValueError, match="source events or agent-bound"):
            await invoke(client, "add", [{"role": "user", "content": CLAIM["quote"]}], "u",
                preference_claims=[CLAIM], **options)
    for options in ({"agent_id": "a"}, {"as_of": datetime.now(UTC)}):
        with pytest.raises(ValueError, match="as_of or agent-bound"):
            await invoke(client, "get", "language", "u", preference_context=CONTEXT, **options)
    assert not captured


@pytest.mark.parametrize("name", ["PreferenceApplicability", "PreferenceClaimRequest", "PreferenceReadContext"])
def test_standalone_schema_matches_backend(name):
    sdk_model = {"PreferenceApplicability": PreferenceApplicability,
        "PreferenceClaimRequest": PreferenceClaimRequest, "PreferenceReadContext": PreferenceReadContext}[name]
    assert sdk_model.model_json_schema() == getattr(backend, name).model_json_schema()


@pytest.mark.parametrize("change", [
    {"turn_index": True}, {"turn_index": 64}, {"property_key": "Invalid"}, {"quote": "No value"},
    {"quote": " Python "}, {"replaces_version": None}, {"replaces_memory_id": "x" * 36},
    {"commitment": "asserted"}, {"authority": 100},
    {"applicability": {"kind": "global", "namespace": "project"}},
    {"applicability": {"kind": "context", "key": "x"}},
])
def test_invalid_declarations_rejected_by_both_contracts(change):
    for cls in (PreferenceClaimRequest, backend.PreferenceClaimRequest):
        with pytest.raises(ValueError):
            cls.model_validate({**CLAIM, **change})


@pytest.mark.parametrize("claims", [[], [CLAIM] * 17])
def test_write_batch_bounds(claims):
    with pytest.raises(ValueError):
        AddRequest(external_user_id="u", messages=[{"role": "user", "content": CLAIM["quote"]}],
            preference_claims=claims)


@pytest.mark.parametrize("context", [
    {"applicability": {"kind": "unresolved"}, "property_keys": ["x"]},
    {**CONTEXT, "property_keys": []}, {**CONTEXT, "property_keys": ["x"] * 2},
    {**CONTEXT, "property_keys": [f"x{i}" for i in range(17)]},
])
def test_invalid_read_context(context):
    for cls in (PreferenceReadContext, backend.PreferenceReadContext):
        with pytest.raises(ValueError):
            cls.model_validate(context)


@pytest.mark.parametrize("value", [None, True, -1, 2.5, "2", {}, 2])
def test_job_capacity_accessor_preserves_raw_metadata(value):
    job = MemoryJobStatus(job_id="j", status="completed",
        extraction_metadata={"source_reviews_capacity_rejected": value})
    assert job.source_reviews_capacity_rejected == (2 if type(value) is int and value == 2 else 0)
    assert job.extraction_metadata["source_reviews_capacity_rejected"] == value
    assert MemoryJobStatus(job_id="j", status="completed").source_reviews_capacity_rejected == 0


def test_management_and_retrieval_preserve_archived_replacement_version():
    provenance = {"preference_version": "c" * 64, "future_field": "keep"}
    record = MemoryRecord(id="m", content="Python", category="preference", importance_score=5,
        is_archived=True, provenance=provenance)
    result = MemoryResult(id="m", content="Python", category="preference", importance_score=5,
        relevance_score=1, context_snippet="Python", provenance=provenance)
    assert record.is_archived and record.provenance == result.provenance == provenance


@pytest.mark.asyncio
async def test_disabled_backend_is_not_silently_retried_as_legacy(asynchronous):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(409, json={"error": "explicit_preferences_unavailable",
            "code": "PREF_409", "request_id": "r"})

    client = AsyncMemory("test") if asynchronous else Memory("test")
    await invoke(client._client, "aclose") if asynchronous else client._client.close()
    cls = httpx.AsyncClient if asynchronous else httpx.Client
    client._client = cls(base_url=client.base_url, transport=httpx.MockTransport(handler))
    try:
        with pytest.raises(MemoryOSError) as error:
            await invoke(client, "get", "language", "u", preference_context=CONTEXT)
        assert error.value.status_code == 409
        assert error.value.code == "PREF_409"
        assert len(calls) == 1
    finally:
        await invoke(client, "close")
