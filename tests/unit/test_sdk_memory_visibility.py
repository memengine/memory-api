"""Real SDK transports preserve backend read status without polling or interpretation."""
import json
import sys
from pathlib import Path

import httpx
import pytest
from pydantic import ValidationError

SDK_PATH = Path(__file__).resolve().parents[2] / "sdk" / "python"
if str(SDK_PATH) not in sys.path:
    sys.path.insert(0, str(SDK_PATH))

from memoryos import AsyncMemory, Memory, MemoryVisibility

from api.schemas.responses import MemoryVisibility as BackendVisibility
from tests.unit.test_sdk_preferences import invoke


@pytest.mark.asyncio
@pytest.mark.parametrize("asynchronous", [False, True], ids=["sync", "async"])
@pytest.mark.parametrize("visibility", [None, {"state": "processing"},
    {"state": "processing", "scope": "affected_context"},
    {"state": "unavailable", "scope": "user_learned_context"},
    {"state": "unavailable", "scope": "affected_context", "future_field": "ignored"}])
@pytest.mark.parametrize("prompt", ["", "Backend withholding notice"])
async def test_visibility_survives_both_clients_and_empty_prompt_budget(asynchronous, visibility, prompt):
    calls = []
    wire = {"request_id": "r", "timestamp": "2026-10-06T00:00:00Z", "data": [],
        "cached": False, "system_prompt_addition": prompt, "quota_mode": "FULL",
        "is_degraded": visibility is not None}
    if visibility is not None:
        wire["memory_visibility"] = visibility
        if visibility.get("scope") == "affected_context":
            wire["data"] = [{"id": "unaffected", "content": "My city is Pune", "category": "fact",
                "importance_score": 5, "relevance_score": 1, "context_snippet": "My city is Pune"}]

    def handler(request):
        calls.append((request.url.path, json.loads(request.content)))
        return httpx.Response(200, json=wire)

    client = AsyncMemory("test") if asynchronous else Memory("test")
    await invoke(client._client, "aclose") if asynchronous else client._client.close()
    cls = httpx.AsyncClient if asynchronous else httpx.Client
    client._client = cls(base_url=client.base_url, transport=httpx.MockTransport(handler))
    try:
        result = await invoke(client, "get", "language", "u")
        expected = BackendVisibility.model_validate(visibility).model_dump() if visibility else None
        assert (result.memory_visibility.model_dump() if result.memory_visibility else None) == expected
        assert isinstance(result.memory_visibility, MemoryVisibility) == (visibility is not None)
        assert result.is_degraded == (visibility is not None) and result.quota_mode == "FULL"
        assert result.system_prompt_addition == prompt and result.has_context == bool(prompt)
        assert [item.id for item in result.items] == [item["id"] for item in wire["data"]]
        assert not result.preference_decisions and not result.source_reviews and result.clarification is None
        assert len(calls) == 1 and calls[0][0] == "/v1/memories/retrieve"
        assert "preference_context" not in calls[0][1]
    finally:
        await invoke(client, "close")


@pytest.mark.parametrize("raw", [{"state": "resolved"}, {"state": "processing", "scope": "all"},
    {"state": "processing", "scope": None}, "processing", []])
def test_invalid_visibility_is_not_silently_dropped(raw):
    with pytest.raises(ValidationError):
        MemoryVisibility.model_validate(raw)


def test_visibility_contract_matches_backend_defaults_and_enums():
    for raw in [{"state": state, "scope": scope} for state in ("processing", "unavailable")
        for scope in ("user_learned_context", "affected_context")]:
        assert MemoryVisibility.model_validate(raw).model_dump() == BackendVisibility.model_validate(raw).model_dump()
