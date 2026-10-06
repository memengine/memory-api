"""Missing-scope wire contract and prompt budgets; no scope/model inference."""
import json
import sys
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from pydantic import ValidationError

SDK_PATH = Path(__file__).resolve().parents[2] / "sdk" / "python"
if str(SDK_PATH) not in sys.path:
    sys.path.insert(0, str(SDK_PATH))

from memoryos import AsyncMemory, Memory
from memoryos import PreferenceScopeRequest as SDKScopeRequest

from api.schemas.preferences import PreferenceScopeRequest
from api.services.context_builder import ContextBuilder
from tests.unit.test_sdk_preferences import invoke
from tests.unit.test_tenant_clarification_answer import _request

REQUEST = {"state": "application_scope_required", "property_keys": ["examples.language"],
    "available_scopes": [{"kind": "global"}, {"kind": "context", "namespace": "project", "key": "Cedar"}]}


@pytest.mark.parametrize("change", [{"property_keys": []}, {"property_keys": ["x", "x"]},
    {"available_scopes": []}, {"available_scopes": [{"kind": "unresolved"}]},
    {"available_scopes": [{"kind": "global"}, {"kind": "global"}]},
    {"state": "resolved"}, {"property_keys": ["x"] * 17}, {"available_scopes": [{"kind": "global"}] * 33}])
def test_scope_contract_rejects_unknown_empty_or_unbounded_data(change):
    for cls in (PreferenceScopeRequest, SDKScopeRequest):
        with pytest.raises(ValidationError):
            cls.model_validate({**REQUEST, **change})


@pytest.mark.parametrize("format", ["bullets", "json", "xml"])
@pytest.mark.parametrize("budget", [0, 2, 500])
def test_scope_notice_is_budgeted_but_typed_request_is_independent(format, budget):
    request = PreferenceScopeRequest.model_validate(REQUEST)
    assert SDKScopeRequest.model_validate(REQUEST).model_dump() == request.model_dump()
    result = ContextBuilder().build_scope_required([], request=request, format=format, max_tokens=budget)
    assert result.token_count <= budget
    if budget == 500:
        assert "do not choose a global or scoped default" in result.system_prompt_addition
        if format == "json":
            assert json.loads(result.system_prompt_addition)["preference_scope_request"] == request.model_dump(mode="json")
    else:
        assert result.system_prompt_addition == ""
    assert request.state == "application_scope_required"


@pytest.mark.asyncio
@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("scope_request", [None, REQUEST])
async def test_scope_request_preserved_without_polling_or_assumed_scope(asynchronous, scope_request):
    calls = []
    def handler(req):
        calls.append(json.loads(req.content))
        return httpx.Response(200, json={"request_id": "r", "timestamp": "2026-10-06T00:00:00Z",
            "data": [], "cached": False, "system_prompt_addition": "", "preference_scope_request": scope_request,
            "clarification_question": "Which project applies?" if scope_request else None})
    client = AsyncMemory("test") if asynchronous else Memory("test")
    await invoke(client._client, "aclose") if asynchronous else client._client.close()
    cls = httpx.AsyncClient if asynchronous else httpx.Client
    client._client = cls(base_url=client.base_url, transport=httpx.MockTransport(handler))
    try:
        result = await invoke(client, "get", "language", "u", context_max_tokens=0)
        expected = PreferenceScopeRequest.model_validate(scope_request).model_dump(mode="json") if scope_request else None
        assert (result.preference_scope_request.model_dump(mode="json") if result.preference_scope_request else None) == expected
        assert not result.has_context and not result.items
        assert len(calls) == 1 and "preference_context" not in calls[0]
    finally:
        await invoke(client, "close")


@pytest.mark.asyncio
@pytest.mark.parametrize("budget", [50, 500])
async def test_actual_route_preserves_missing_scope_and_does_not_pop_legacy_clarification(monkeypatch, budget):
    from api.routers import memories
    from api.schemas.requests import MemoryRetrieveRequest
    scope_request = PreferenceScopeRequest.model_validate(REQUEST)
    monkeypatch.setattr(memories, "_tenant_domain_schema", AsyncMock(return_value=None))
    monkeypatch.setattr(memories, "list_source_reviews", AsyncMock(return_value=[]))
    old_cards = AsyncMock(side_effect=AssertionError("legacy clarification must not select scope"))
    monkeypatch.setattr(memories, "_pop_next_clarification", old_cards)
    monkeypatch.setattr(memories, "RetrievalFeedbackService", lambda **_: SimpleNamespace(
        log_retrieval=AsyncMock(return_value=SimpleNamespace(id=uuid.uuid4()))))
    service = SimpleNamespace(retrieve=AsyncMock(return_value=[]), last_cache_hit=False,
        last_preference_scope_request=scope_request, last_quota_mode="FULL", last_is_degraded=False)
    response = await memories.retrieve_memories(_request(), MemoryRetrieveRequest(
        external_user_id="u", query="language", context_max_tokens=budget), service,
        SimpleNamespace(resolve=AsyncMock(return_value=SimpleNamespace(id=uuid.uuid4()))),
        ContextBuilder(), AsyncMock(), AsyncMock(), str(uuid.uuid4()))
    assert response.preference_scope_request == scope_request and response.clarification is None
    assert response.memory_visibility is None and "no default" in response.clarification_question
    assert bool(response.system_prompt_addition) == (budget == 500)
    old_cards.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("first_item", [None, "invalid", [], {}, {"_preference_nominee_ids": "invalid"}])
async def test_discovery_missing_or_malformed_cache_metadata_is_a_miss(monkeypatch, first_item):
    from api.services import retriever

    monkeypatch.setattr(retriever, "REDIS_CACHE_READ_ENABLED", True)
    service = object.__new__(retriever.RetrieverService)
    service._preference_discovery_active = True
    service._preference_nominee_ids = []
    service._preference_cold_start = False
    service._read_retrieval_cache = AsyncMock(return_value=[first_item])
    assert await service._get_cached_results(user_id=str(uuid.uuid4()), cache_context="discovery") is None
    assert service._preference_nominee_ids == [] and not service._preference_cold_start
