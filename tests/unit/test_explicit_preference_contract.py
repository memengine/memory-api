from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError

from api.errors import APIError
from api.routers.memories import add_memories, retrieve_memories
from api.schemas.preferences import (
    PreferenceApplicability,
    PreferenceClaimRequest,
    PreferenceReadContext,
)
from api.schemas.requests import MemoryAddRequest, MemoryRetrieveRequest
from api.services.preference_claim_service import (
    declared_preferences,
    preference_identity,
    transition_reason,
)
from api.settings import Settings, get_settings


def claim(**kwargs):
    return PreferenceClaimRequest.model_validate({
        "property_key": "examples.language", "value": "C++",
        "applicability": {"kind": "global"}, "turn_index": 0,
        "quote": "My examples should use C++.", **kwargs,
    })


@pytest.mark.parametrize("scope", [
    {"kind": "global", "key": "x"}, {"kind": "context"},
    {"kind": "context", "namespace": "project"},
    {"kind": "unresolved", "namespace": "project", "key": "x"},
    {"kind": "global", "authority": 100},
])
def test_scope_is_explicit_and_closed(scope):
    with pytest.raises(ValidationError):
        PreferenceApplicability.model_validate(scope)


def test_identity_is_not_wording_or_value_dependent_and_preserves_case_sensitive_context_keys():
    original = preference_identity(claim())
    different_wording = preference_identity(claim(quote="Use C++ for examples please."))
    different_value = preference_identity(claim(value="Python", quote="Use Python for examples."))
    assert original.fingerprint == different_wording.fingerprint == different_value.fingerprint
    project = preference_identity(claim(applicability={"kind": "context", "namespace": "project", "key": "Release"}))
    assert project.fingerprint != original.fingerprint
    assert project.fingerprint != preference_identity(claim(applicability={"kind": "context", "namespace": "project", "key": "release"})).fingerprint


@pytest.mark.parametrize("role,kind", [
    ("assistant", "assistant_output"), ("system", "system_instruction"),
    ("user", "tool_output"), ("user", "fetched_document"),
])
def test_forged_user_evidence_does_not_admit_tools_or_documents(role, kind):
    with pytest.raises(ValueError):
        declared_preferences([claim()], [{"role": role, "source_kind": kind, "content": claim().quote}])


@pytest.mark.parametrize("content", ["Nothing about this preference.", "My examples should use C++. My examples should use C++."])
def test_quote_must_exist_uniquely(content):
    with pytest.raises(ValueError):
        declared_preferences([claim()], [{"role": "user", "content": content}])


def test_no_request_model_can_assign_authority_or_inject_identity_metadata():
    with pytest.raises(ValidationError):
        claim(authority=100)
    with pytest.raises(ValidationError):
        claim(value="Python")
    with pytest.raises(ValueError):
        declared_preferences([claim(), claim()], [{"role": "user", "content": claim().quote}])
    assert Settings(_env_file=None).explicit_preference_claims_enabled is False


def test_contract_is_opt_in_and_history_cannot_silently_ignore_scope():
    assert MemoryAddRequest(external_user_id="u", messages=[{"content": "Hello"}]).preference_claims is None
    context = {"applicability": {"kind": "global"}, "property_keys": ["examples.language"]}
    with pytest.raises(ValidationError):
        MemoryRetrieveRequest(external_user_id="u", query="language?", preference_context=context, as_of="2026-10-03T00:00:00Z")
    with pytest.raises(ValidationError):
        PreferenceReadContext.model_validate({**context, "property_keys": ["x"] * 17})
    with pytest.raises(ValidationError):
        PreferenceReadContext.model_validate({**context, "property_keys": ["x", "x"]})


@pytest.mark.parametrize("commitment,scope", [
    ("uncertain", {"kind": "global"}), ("asserted", {"kind": "unresolved"}),
])
def test_uncertainty_never_activates(commitment, scope):
    assert transition_reason(claim(commitment=commitment, applicability=scope), SimpleNamespace(active_memory_id=None), None) == "preference_requires_review"


@pytest.mark.asyncio
async def test_disabled_contract_rejects_before_queue_or_retrieval(monkeypatch):
    monkeypatch.setattr(get_settings(), "explicit_preference_claims_enabled", False)
    request = SimpleNamespace()
    service, proxies, quality = AsyncMock(), AsyncMock(), AsyncMock()
    payload = MemoryAddRequest(
        external_user_id="u", messages=[{"content": claim().quote}], preference_claims=[claim()],
    )
    with pytest.raises(APIError) as add_error:
        await add_memories(request, SimpleNamespace(), payload, service, proxies, quality, tenant_id="t", idempotency_key=None)
    assert add_error.value.status_code == 409
    service.queue_memory_add.assert_not_called()
    proxies.resolve.assert_not_called()
    retrieval = AsyncMock()
    with pytest.raises(APIError) as retrieve_error:
        await retrieve_memories(
            request, MemoryRetrieveRequest(external_user_id="u", query="language?", preference_context={
                "applicability": {"kind": "global"}, "property_keys": ["examples.language"],
            }), retrieval, proxies, AsyncMock(), AsyncMock(), AsyncMock(), tenant_id="t",
        )
    assert retrieve_error.value.status_code == 409
    retrieval.retrieve.assert_not_called()


@pytest.mark.asyncio
async def test_mcp_lane_cannot_enable_explicit_preferences(monkeypatch):
    monkeypatch.setattr(get_settings(), "explicit_preference_claims_enabled", True)
    with pytest.raises(APIError):
        await add_memories(
            SimpleNamespace(), SimpleNamespace(), MemoryAddRequest(external_user_id="u", messages=[{"content": claim().quote}], preference_claims=[claim()]),
            AsyncMock(), AsyncMock(), AsyncMock(), tenant_id="t", idempotency_key=None,
            trusted_submission_kind="mcp_client_assertion",
        )
