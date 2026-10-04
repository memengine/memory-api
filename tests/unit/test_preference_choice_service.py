"""Internal contracts and attribution, not a natural-language quality eval."""
import uuid

import pytest
from pydantic import ValidationError

from api.schemas.preferences import PreferenceClaimRequest
from api.services.preference_choice_service import (
    CHOICE_OPERATION_MARKER,
    PreferenceChoiceRequest,
    bind_choice_operations,
)
from api.services.preference_claim_service import preference_fingerprint
from api.services.preference_snapshot_service import (
    PreferenceIdentitySnapshot,
    PreferenceSnapshot,
    PreferenceTargetSnapshot,
)
from tests.unit.test_preference_snapshot_service import catalog


def request(**changes):
    return {"action": "withhold", "property_key": "examples.language", "scope_ref": "global",
        "turn_index": 0, "quote": "I haven't decided which default to keep.", **changes}


def snapshot(*, absent=False):
    offered = catalog()
    target = None if absent else PreferenceTargetSnapshot(uuid.uuid4(), "C++", "a" * 64, 20, True, "current")
    return PreferenceSnapshot(offered.tenant_id, uuid.uuid4(), offered, (
        PreferenceIdentitySnapshot("examples.language", "global",
            preference_fingerprint("examples.language", offered.scopes[0].applicability),
            None if absent else uuid.uuid4(), target, "b" * 64),
    ), "c" * 64)


def bind(snap=None, messages=None, requests=None):
    return bind_choice_operations(requests if requests is not None else [request()], snapshot=snap or snapshot(),
        messages=messages if messages is not None else [{"role": "user", "content": request()["quote"]}],
        source_conversation_id=uuid.uuid4())


def test_value_free_descriptor_and_actual_user_span():
    snap = snapshot()
    memory = bind(snap)[0]
    descriptor = memory.validated_evidence[CHOICE_OPERATION_MARKER]
    assert "value" not in descriptor and "C++" not in memory.content
    assert descriptor["target_memory_id"] == str(snap.identities[0].target.memory_id)
    assert descriptor["binding_version"] == snap.identities[0].binding_version
    assert memory.validated_evidence["grounding_mode"] == "verified_source_spans"
    assert memory.validated_evidence["preference_fingerprint"] == snap.identities[0].fingerprint


@pytest.mark.parametrize("changes", [
    {"value": "Python"}, {"authority": 100}, {"target_memory_id": str(uuid.uuid4())},
    {"action": "replace"}, {"turn_index": True}, {"turn_index": 64},
    {"quote": " "}, {"quote": " spaced "}, {"scope_ref": "unknown"}, {"property_key": "unknown"},
])
def test_invalid_or_unoffered_request_rejected(changes):
    with pytest.raises(ValueError):
        bind(requests=[request(**changes)])


@pytest.mark.parametrize("messages", [
    [{"role": role, "content": request()["quote"]}] for role in ("assistant", "tool", "system")
] + [[{"role": "user", "content": "Different evidence."}],
     [{"role": "user", "source_kind": "tool_result", "content": request()["quote"]}]])
def test_forged_or_missing_user_evidence_rejected(messages):
    with pytest.raises(ValueError):
        bind(messages=messages)


@pytest.mark.parametrize("requests", [[], [request()] * 2, [request()] * 17])
def test_batch_bound_and_duplicate_identity(requests):
    with pytest.raises(ValueError):
        bind(requests=requests)


def test_absence_is_not_a_choice_target():
    with pytest.raises(ValueError, match="target unavailable"):
        bind(snapshot(absent=True))


def test_public_declaration_still_requires_verbatim_value():
    with pytest.raises(ValidationError):
        PreferenceClaimRequest.model_validate({"property_key": "examples.language",
            "applicability": {"kind": "global"}, "turn_index": 0, "quote": request()["quote"]})
    assert PreferenceChoiceRequest.model_validate(request()).quote == request()["quote"]
