"""Strict optional backend contract; no semantic classifier or SDK inference."""
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError

from api.routers.memories import retrieve_memories
from api.schemas.preferences import PreferenceReadDecision
from api.schemas.requests import MemoryRetrieveRequest
from api.schemas.responses import MemoryRetrieveResponse
from api.services.context_builder import ContextBuilder
from api.settings import get_settings
from tests.unit.test_explicit_preference_contract import claim
from tests.unit.test_tenant_clarification_answer import _request


def declaration(**kwargs):
    return claim(commitment="uncertain", choice_state="current_choice_unresolved",
        replaces_memory_id=str(uuid.uuid4()), replaces_version="a" * 64, **kwargs)


@pytest.mark.parametrize("change", [
    {"commitment": "asserted"}, {"commitment": "replacement"},
    {"replaces_memory_id": None}, {"replaces_version": None},
    {"applicability": {"kind": "unresolved"}}, {"choice_state": "model_decides"},
    {"choice_state": "candidate_only"},
])
def test_withholding_requires_known_scope_and_complete_snapshot(change):
    base = declaration().model_dump()
    with pytest.raises(ValidationError):
        type(claim()).model_validate({**base, **change})


def test_default_is_candidate_only_and_decisions_are_bounded():
    assert claim(commitment="uncertain").choice_state == "candidate_only"
    decision = PreferenceReadDecision(property_key="examples.language", applicability={"kind": "global"})
    assert set(decision.model_dump()) == {"property_key", "applicability", "state"}
    with pytest.raises(ValidationError):
        PreferenceReadDecision.model_validate({**decision.model_dump(), "value": "C++"})
    with pytest.raises(ValidationError):
        MemoryRetrieveResponse(data=[], cached=False, request_id="r", timestamp="2026-10-03T00:00:00Z",
            preference_decisions=[decision] * 17)


@pytest.mark.asyncio
@pytest.mark.parametrize("explicit", [True, False])
async def test_read_exposes_policy_without_putting_withheld_value_into_context(monkeypatch, explicit):
    from api.routers import memories

    monkeypatch.setattr(get_settings(), "explicit_preference_claims_enabled", True)
    monkeypatch.setattr(memories, "_tenant_domain_schema", AsyncMock(return_value=None))
    monkeypatch.setattr(memories, "list_source_reviews", AsyncMock(return_value=[]))
    monkeypatch.setattr(memories, "_pop_next_clarification", AsyncMock(return_value=None))
    monkeypatch.setattr(memories, "RetrievalFeedbackService", lambda **_: SimpleNamespace(
        log_retrieval=AsyncMock(return_value=SimpleNamespace(id=uuid.uuid4()))))
    decision = PreferenceReadDecision(property_key="examples.language", applicability={"kind": "global"})
    service = SimpleNamespace(retrieve=AsyncMock(return_value=[]), last_cache_hit=False,
        last_preference_decisions=[decision], last_quota_mode="DEGRADED", last_is_degraded=True)
    proxies = SimpleNamespace(resolve=AsyncMock(return_value=SimpleNamespace(id=uuid.uuid4())))
    payload = MemoryRetrieveRequest(external_user_id="u", query="language?", categories=["preference"],
        **({"preference_context": {"applicability": {"kind": "global"},
            "property_keys": ["examples.language"]}} if explicit else {}))
    response = await retrieve_memories(_request(), payload, service, proxies, ContextBuilder(),
        AsyncMock(), AsyncMock(), str(uuid.uuid4()))
    assert response.data == [] and "C++" not in (response.system_prompt_addition or "")
    assert response.preference_decisions == ([decision] if explicit else [])
    assert bool(response.clarification_question) is explicit
