"""Typed registration, one SDK request, no hidden polling or fallback."""
import json
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from pydantic import ValidationError

from api.schemas.requests import MemoryPrepareTurnRequest
from api.schemas.responses import TurnAdmission
from api.services.budget_governor import BudgetGovernor
from api.services.quality_gate import QualityGateService
from tests.unit.test_quality_gate import FakeRedis, make_tenant_budget
from tests.unit.test_sdk_preferences import invoke

from memoryos import AsyncMemory, Memory
from memoryos.types import TurnAdmission as SDKAdmission


@pytest.mark.parametrize("changes", [{"message": " "}, {"turn_id": " "}, {"conversation_id": " "},
    {"as_of": "2026-10-06T00:00:00Z"}, {"agent_id": "a"}, {"authority": 100},
    {"preceding_assistant": {"role": "user", "content": "fake", "source_kind": "client_assertion"}},
    {"preceding_assistant": {"role": "assistant", "content": "fake", "source_kind": "tool_output"}}])
def test_turn_request_is_current_bounded_and_not_a_trust_override(changes):
    with pytest.raises(ValidationError):
        MemoryPrepareTurnRequest.model_validate({"external_user_id": "u", "query": "q", "message": "No",
            "conversation_id": "c", "turn_id": "t", **changes})


@pytest.mark.parametrize("value", [{"state": "accepted"}, {"state": "accepted", "job_id": "not-a-uuid"},
    {"state": "accepted", "job_id": str(uuid.uuid4()), "reason": "failed"},
    {"state": "rejected", "job_id": str(uuid.uuid4())}, {"state": "resolved"}, {"state": "rejected", "authority": 100}])
def test_invalid_admission_is_not_silently_ignored(value):
    for cls in (TurnAdmission, SDKAdmission):
        with pytest.raises(ValidationError):
            cls.model_validate(value)


@pytest.mark.asyncio
@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("state", ["accepted", "replayed", "rejected", "unavailable"])
async def test_sdk_turn_transport_preserves_status_without_waits(asynchronous, state):
    admission = {"state": state, "job_id": str(uuid.uuid4()) if state in {"accepted", "replayed"} else None}
    captured = []
    def respond(request):
        captured.append((request.url.path, json.loads(request.content)))
        MemoryPrepareTurnRequest.model_validate(captured[-1][1])
        return httpx.Response(200, json={"admission": admission, "data": [], "cached": False,
            "system_prompt_addition": "", "memory_visibility": {"state": "processing"}, "is_degraded": True,
            "request_id": "r", "timestamp": "2026-10-06T00:00:00Z"})
    client = AsyncMemory("test") if asynchronous else Memory("test")
    await invoke(client._client, "aclose") if asynchronous else client._client.close()
    cls = httpx.AsyncClient if asynchronous else httpx.Client
    client._client = cls(base_url=client.base_url, transport=httpx.MockTransport(respond))
    try:
        result = await invoke(client, "prepare_turn", "No", "u", conversation_id="c", turn_id="t")
        assert result.admission.state == state and result.memory_visibility.state == "processing"
        assert result.is_degraded and not result.has_context
        assert captured[0][0] == "/v1/memories/prepare-turn" and len(captured) == 1
        assert captured[0][1]["query"] == "No"
    finally:
        await invoke(client, "close")


@pytest.mark.asyncio
@pytest.mark.parametrize("reason", [None, "rate", "budget"])
async def test_turn_admission_skips_semantic_quality_but_preserves_limits(reason):
    budget = make_tenant_budget(rate_limit_per_user_per_minute=1 if reason == "rate" else 10,
        monthly_call_limit=0 if reason == "budget" else 1000)
    from api.db.models import OveragePolicy
    budget.overage_policy = OveragePolicy.block
    session = MagicMock()
    session.commit = AsyncMock()
    governor = BudgetGovernor(session=session, dispatch_task=MagicMock(side_effect=AssertionError("no broker during admission")))
    governor.get_tenant_budget = AsyncMock(return_value=budget)
    embedding = SimpleNamespace(embed=AsyncMock(side_effect=AssertionError("no read-time semantic classifier")))
    gate = QualityGateService(session=session, cache_service=SimpleNamespace(client=FakeRedis()),
        budget_governor=governor, embedding_service=embedding)
    tenant_id = str(uuid.uuid4())
    result = await gate.check([{"role": "user", "content": "No"}], tenant_id, "u", turn_admission=True)
    if reason == "rate":
        assert result.passed
        result = await gate.check([{"role": "user", "content": "No"}], tenant_id, "u", turn_admission=True)
    assert result.passed == (reason is None)
    if reason:
        assert result.reason == ("rate_limit_exceeded" if reason == "rate" else "budget_exhausted")
    embedding.embed.assert_not_awaited()
    governor.dispatch_task.assert_not_called()
