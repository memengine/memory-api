"""Pure runtime contracts; controlled intents do not prove model accuracy."""
import uuid
from dataclasses import replace

import pytest
from pydantic import ValidationError

from api.schemas.preferences import PreferenceClaimRequest
from api.services.preference_choice_service import CHOICE_OPERATION_MARKER
from api.services.preference_claim_service import PREFERENCE_MARKER
from api.services.preference_intent_contract import (
    PreferenceIntentReply,
    bind_preference_intents,
)
from api.services.preference_snapshot_service import _digest, snapshot_binding_version
from tests.unit.test_preference_choice_service import snapshot


def offered(*, absent=False):
    result = snapshot(absent=absent)
    if absent:
        identity = result.identities[0]
        result = replace(result, identities=(replace(identity, binding_version=_digest({"absent": identity.fingerprint})),))
    return replace(result, binding_version=snapshot_binding_version(result))


def intent(**changes):
    return {"action": "set_current", "property_key": "examples.language", "scope_ref": "global",
        "value": "Python", "turn_index": 0, "quote": "Use Python instead of C++", **changes}


def reply(preferences=None, nonpreferences=None):
    return {"preferences": [intent()] if preferences is None else preferences,
        "nonpreferences": [] if nonpreferences is None else nonpreferences}


def bind(raw=None, *, snap=None, messages=None):
    return bind_preference_intents(reply() if raw is None else raw, snapshot=snap or offered(),
        messages=messages or [{"role": "user", "content": intent()["quote"]}], source_conversation_id=uuid.uuid4())


@pytest.fixture(autouse=True)
def no_providers(monkeypatch):
    def forbidden(*_args, **_kwargs):
        pytest.fail("binding must not classify or embed")
    monkeypatch.setattr("api.services.llm_service.LLMService.complete_sync", forbidden)
    monkeypatch.setattr("api.services.embedding_service.EmbeddingService.embed_sync", forbidden)


def test_mixed_correction_retains_unrelated_fact_and_backend_target():
    text = "Use Python instead of C++, and my exam moved to October 18."
    snap = offered()
    result = bind(reply(nonpreferences=[{"category": "fact", "content": "my exam moved to October 18",
        "turn_index": 0, "quote": "my exam moved to October 18"}]), snap=snap, messages=[{"role": "user", "content": text}])
    assert len(result.declarations) == len(result.nonpreferences) == 1
    assert not result.choices and not result.pending_preferences
    declaration = result.declarations[0].validated_evidence[PREFERENCE_MARKER]
    assert declaration["commitment"] == "replacement"
    assert declaration["replaces_memory_id"] == str(snap.identities[0].target.memory_id)
    assert declaration["replaces_version"] == snap.identities[0].target.memory_version
    assert result.nonpreferences[0].content == "my exam moved to October 18"
    assert PREFERENCE_MARKER not in result.nonpreferences[0].validated_evidence
    assert result.snapshot is snap


@pytest.mark.parametrize("action", ["assert", "set_current"])
def test_new_preference_never_invents_replacement_reference(action):
    result = bind(reply([intent(action=action)]), snap=offered(absent=True))
    declared = result.declarations[0].validated_evidence[PREFERENCE_MARKER]
    assert declared["commitment"] == "asserted"
    assert "replaces_memory_id" not in declared and "replaces_version" not in declared


def test_asserting_different_value_is_not_automatic_replacement():
    result = bind(reply([intent(action="assert")]))
    assert result.declarations[0].validated_evidence[PREFERENCE_MARKER]["commitment"] == "asserted"


@pytest.mark.parametrize("action", ["withhold", "reaffirm"])
@pytest.mark.parametrize("text", ["I cannot settle on either default yet.", "दोनों में से अभी तय नहीं किया।", "abhi decide nahi hua"])
def test_value_free_choices_do_not_depend_on_regex_or_alternative_value(action, text):
    result = bind(reply([intent(action=action, value=None, quote=text)]), messages=[{"role": "user", "content": text}])
    assert not result.declarations and len(result.choices) == 1
    operation = result.choices[0].validated_evidence[CHOICE_OPERATION_MARKER]
    assert operation["action"] == action and "value" not in operation
    assert operation["target_memory_id"] == str(result.snapshot.identities[0].target.memory_id)


@pytest.mark.parametrize("action", ["withhold", "reaffirm"])
def test_absent_choice_is_pending_not_a_fabricated_preference(action):
    result = bind(reply([intent(action=action, value=None)]), snap=offered(absent=True))
    assert len(result.pending_preferences) == 1
    assert not result.declarations and not result.choices


@pytest.mark.parametrize("action", ["tentative", "contextual", "abstain"])
def test_noncommitted_preferences_never_become_defaults(action):
    result = bind(reply([intent(action=action, value=None)]))
    assert len(result.pending_preferences) == 1 and not result.declarations and not result.choices


def test_unrepresentable_preference_preserved_without_inventing_identity():
    result = bind(reply([intent(action="abstain", property_key=None, scope_ref=None, value=None)]))
    assert result.pending_preferences[0].content == intent()["quote"]


@pytest.mark.parametrize("changes", [
    {"authority": 100}, {"confidence": 0.99}, {"replaces_memory_id": str(uuid.uuid4())},
    {"applicability": {"kind": "global"}}, {"action": "replace"},
    {"turn_index": True}, {"turn_index": 64}, {"quote": " padded "},
    {"value": "Rust"}, {"value": None}, {"property_key": "unknown"}, {"scope_ref": "unknown"},
    {"property_key": None}, {"action": "withhold"}, {"action": "abstain"},
])
def test_invalid_or_unoffered_output_rejected(changes):
    with pytest.raises(ValueError):
        bind(reply([intent(**changes)]))


@pytest.mark.parametrize("role,kind", [
    ("assistant", None), ("system", None), ("tool", None),
    ("user", "tool_result"), ("user", "fetched_document"),
])
def test_non_user_or_untrusted_source_cannot_forge_evidence(role, kind):
    with pytest.raises(ValueError):
        bind(messages=[{"role": role, "source_kind": kind, "content": intent()["quote"]}])


@pytest.mark.parametrize("which", ["preference", "fact"])
def test_missing_quote_rejected(which):
    raw = reply() if which == "preference" else reply([], [{"category": "fact", "content": "exam October 18",
        "quote": "exam October 18", "turn_index": 0}])
    with pytest.raises(ValueError):
        bind(raw, messages=[{"role": "user", "content": "Unrelated statement."}])


@pytest.mark.parametrize("category", ["preference", "unsupported"])
def test_fact_lane_rejects_preference_category(category):
    with pytest.raises(ValidationError):
        PreferenceIntentReply.model_validate(reply([], [{"category": category,
            "content": "Python", "quote": "Python", "turn_index": 0}]))


def test_preference_cannot_be_processed_again_as_fact():
    with pytest.raises(ValidationError, match="fact lane"):
        PreferenceIntentReply.model_validate(reply(nonpreferences=[{"category": "fact",
            "content": "Use Python instead of C++", "quote": "Use Python instead of C++", "turn_index": 0}]))


def test_combined_limit_duplicate_identity_and_duplicate_fact():
    for raw in (reply([intent()] * 2), reply([], [{"category": "fact", "content": "x", "quote": "x", "turn_index": 0}] * 2),
                reply([intent()], [{"category": "fact", "content": str(i), "quote": str(i), "turn_index": 0} for i in range(16)])):
        with pytest.raises(ValidationError):
            PreferenceIntentReply.model_validate(raw)


def test_empty_reply_is_valid_and_public_declaration_unchanged():
    result = bind(reply([], []))
    assert not any((result.declarations, result.choices, result.pending_preferences, result.nonpreferences))
    with pytest.raises(ValidationError):
        PreferenceClaimRequest.model_validate(intent())


def test_invalid_snapshot_fails_closed_before_binding():
    with pytest.raises(ValueError, match="snapshot coverage"):
        bind(snap=replace(offered(), binding_version="a" * 64))


def test_round_trip_and_frozen_closed_schema():
    result = PreferenceIntentReply.model_validate(reply())
    assert PreferenceIntentReply.model_validate_json(result.model_dump_json()) == result
    with pytest.raises(ValidationError):
        result.preferences[0].action = "assert"
    schema = PreferenceIntentReply.model_json_schema()
    assert schema["additionalProperties"] is False
    assert set(schema["required"]) == {"preferences", "nonpreferences"}
    assert all(definition.get("additionalProperties") is False for definition in schema["$defs"].values())
