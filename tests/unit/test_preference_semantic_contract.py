"""Offline policy/attribution checks, not tests of language understanding."""
import hashlib
import json
import uuid
from copy import deepcopy
from pathlib import Path

import pytest
from pydantic import ValidationError

from benchmarks.internal.preference_semantic_contract import (
    ContractContext,
    model_input,
    response_schema,
    validate_reply,
)

TENANT = str(uuid.UUID(int=1))
PROFILE = str(uuid.UUID(int=2))
MEMORY = str(uuid.UUID(int=3))


def context(*, text="I have not decided which default to keep.", role="user", source_kind=None):
    return ContractContext.model_validate({
        "principal": {"tenant_id": TENANT, "profile_id": PROFILE},
        "properties": [{"key": "examples.language", "description": "Default language for programming examples."}],
        "scopes": [
            {"ref": "global", "applicability": {"kind": "global"}, "description": "General default."},
            {"ref": "release", "applicability": {"kind": "context", "namespace": "project", "key": "Release"},
                "description": "Release project only."},
        ],
        "targets": [{"ref": "t0", "memory_id": MEMORY, "tenant_id": TENANT, "profile_id": PROFILE,
            "property_key": "examples.language", "scope_ref": "global", "value": "C++",
            "version": "a" * 64, "authority": 20, "is_current": True}],
        "turns": [{"role": role, "content": text, **({"source_kind": source_kind} if source_kind else {})}],
    })


def assessment(shown, **changes):
    return {"kind": "assessment", "property_key": "examples.language", "scope_ref": "global",
        "target_ref": "t0", "stance": "current_choice_unresolved",
        "evidence": {"turn_index": 0, "quote": shown.turns[0].content}, "value": None, **changes}


def check(shown=None, *, current=None, changes=None, findings=None):
    shown = shown or context()
    return validate_reply({"findings": findings or [assessment(shown, **(changes or {}))]},
        shown=shown, current=current or shown)


def changed(shown, section, **updates):
    raw = shown.model_dump(mode="json")
    if section in {"principal"}:
        raw[section].update(updates)
    else:
        raw[section][0].update(updates)
    return ContractContext.model_validate(raw)


def test_unresolved_choice_does_not_require_or_invent_an_alternative():
    shown = context()
    before = shown.model_dump()
    result = check(shown)
    assert result.status == "attributed_proposals_only"
    assert result.assessments[0].value is None
    assert not result.automatic_activation_allowed
    assert shown.model_dump() == before


def test_interpretation_input_omits_backend_authority_and_persistence_ids():
    shown = context()
    payload = model_input(shown)
    encoded = json.dumps(payload)
    for private in (TENANT, PROFILE, MEMORY, "a" * 64, "authority", "is_current", "automatic_activation_allowed"):
        assert private not in encoded
    assert payload["existing_targets"][0]["ref"] == "t0"
    assert payload["turns"][0]["content"] == shown.turns[0].content


def test_prefilter_only_uses_permissions_and_evidence_eligibility():
    assert model_input(context(role="tool")) is None
    assert model_input(changed(context(), "principal", blocked=True)) is None
    assert model_input(context(text="Could you use Python now, and when is my exam?")) is not None


def test_response_schema_binds_only_offered_catalog_references():
    shown = context()
    fields = response_schema(shown)["$defs"]["Assessment"]["properties"]
    assert fields["property_key"]["enum"] == ["examples.language"]
    assert fields["scope_ref"]["enum"] == ["global", "release"]
    assert fields["target_ref"]["anyOf"][0]["enum"] == ["t0"]
    raw = shown.model_dump(mode="json")
    raw["targets"] = []
    assert response_schema(ContractContext.model_validate(raw))["$defs"]["Assessment"]["properties"]["target_ref"] == {"type": "null"}


@pytest.mark.parametrize("text", [
    "I have not decided which default to keep.",
    "अभी तय नहीं किया कि कौन सा डिफ़ॉल्ट रखूँ।",
    "Abhi decide nahi kiya kaunsa default rakhna hai.",
    "🤔 Python sounds interesting, but I have not changed my default.",
])
def test_unicode_attribution_is_not_semantic_recognition(text):
    # Stance is deliberately supplied; no model or phrase rule inferred it.
    result = check(context(text=text), changes={"stance": "tentative_interest"})
    assert result.status == "attributed_proposals_only"
    assert result.assessments[0].stance == "tentative_interest"
    assert not result.automatic_activation_allowed


@pytest.mark.parametrize("stance", ["asserted", "replacement"])
def test_settled_value_proposals_require_separate_value_evidence(stance):
    shown = context(text="My default is now Python.")
    assert check(shown, changes={"stance": stance}).reasons == ("invalid_reply_shape",)
    result = check(shown, changes={"stance": stance, "value": {"turn_index": 0, "quote": "Python"}})
    assert result.status == "attributed_proposals_only" and not result.automatic_activation_allowed


def test_a_supported_value_does_not_establish_the_commitment_label():
    shown = context(text="I do not want Python as my default.")
    result = check(shown, changes={"stance": "replacement", "value": {"turn_index": 0, "quote": "Python"}})
    # The structural checks pass a semantically wrong label. An independent
    # semantic evaluator must fail it; this validator cannot promote it.
    assert result.status == "attributed_proposals_only"
    assert not result.automatic_activation_allowed


@pytest.mark.parametrize("changes,reason", [
    ({"property_key": "unknown.property"}, "identity_not_offered"),
    ({"scope_ref": "imagined_scope"}, "identity_not_offered"),
    ({"scope_ref": "release"}, "target_identity_mismatch"),
    ({"target_ref": "invented"}, "target_missing_stale_or_ineligible"),
    ({"target_ref": None}, "invalid_reply_shape"),
    ({"stance": "asserted", "target_ref": None, "value": {"turn_index": 0, "quote": "default"}}, "existing_target_omitted"),
    ({"value": {"turn_index": 0, "quote": "Python"}}, "value_outside_evidence"),
    ({"value": {"turn_index": 1, "quote": "default"}}, "value_outside_evidence"),
    ({"evidence": {"turn_index": True, "quote": "default"}}, "invalid_reply_shape"),
    ({"evidence": {"turn_index": 63, "quote": "default"}}, "invalid_source_span_turn"),
    ({"evidence": {"turn_index": 0, "quote": "Fabricated statement."}}, "quote_not_found"),
    ({"authority": 100}, "invalid_reply_shape"),
    ({"confidence": 0.99}, "invalid_reply_shape"),
    ({"automatic_activation_allowed": True}, "invalid_reply_shape"),
    ({"memory_id": MEMORY}, "invalid_reply_shape"),
])
def test_model_cannot_invent_identity_evidence_or_authority(changes, reason):
    result = check(changes=changes)
    assert result.status == "rejected" and result.reasons == (reason,)
    assert result.assessments == () and not result.automatic_activation_allowed


@pytest.mark.parametrize("role,source_kind", [
    ("assistant", None), ("tool", None), ("document", None), ("system", None),
    ("user", "tool_output"), ("user", "fetched_document"), ("user", "assistant_output"),
])
def test_source_role_is_checked_independently_of_the_supplied_label(role, source_kind):
    shown = context(role=role, source_kind=source_kind)
    assert check(shown).reasons == ("invalid_source_span_turn",)


def test_repeated_quotes_are_not_accepted_as_unique_evidence():
    shown = context(text="Not decided. Not decided.")
    assert check(shown, changes={"evidence": {"turn_index": 0, "quote": "Not decided."}}).reasons == ("quote_not_unique",)


@pytest.mark.parametrize("section,updates,reason", [
    ("principal", {"blocked": True}, "principal_changed_or_blocked"),
    ("principal", {"profile_id": str(uuid.UUID(int=10))}, "principal_changed_or_blocked"),
    ("targets", {"version": "b" * 64}, "target_missing_stale_or_ineligible"),
    ("targets", {"authority": 100}, "target_missing_stale_or_ineligible"),
    ("targets", {"is_current": False}, "target_missing_stale_or_ineligible"),
    ("targets", {"choice_state": "current_choice_unresolved"}, "target_missing_stale_or_ineligible"),
    ("properties", {"description": "A changed meaning."}, "catalog_changed"),
    ("scopes", {"description": "A changed scope."}, "catalog_changed"),
    ("turns", {"content": "Source changed."}, "source_changed"),
])
def test_a_fresh_snapshot_must_match_the_shown_input(section, updates, reason):
    shown = context()
    raw = shown.model_dump(mode="json")
    # Change principal and its target ownership together to keep the context valid.
    if section == "principal" and "profile_id" in updates:
        raw["targets"][0].update(profile_id=updates["profile_id"])
    if section == "principal":
        raw[section].update(updates)
    else:
        raw[section][0].update(updates)
    current = ContractContext.model_validate(raw)
    assert check(shown, current=current).reasons == (reason,)


@pytest.mark.parametrize("stance", ["replacement", "current_choice_unresolved", "reaffirmed"])
def test_application_assertion_cannot_propose_overriding_a_higher_authority_target(stance):
    shown = changed(context(text="Python"), "targets", authority=100)
    value = {"turn_index": 0, "quote": "Python"} if stance == "replacement" else None
    assert check(shown, changes={"stance": stance, "value": value}).reasons == ("authority_insufficient",)
    assert check(shown, changes={"stance": "tentative_interest"}).status == "attributed_proposals_only"


def test_new_known_scope_does_not_rebind_a_global_target():
    shown = context(text="Python for Release only.")
    result = check(shown, changes={"stance": "asserted", "scope_ref": "release", "target_ref": None,
        "value": {"turn_index": 0, "quote": "Python"}})
    assert result.status == "attributed_proposals_only"
    assert result.assessments[0].target_ref is None


def test_batch_failure_is_all_or_nothing_and_duplicate_identity_is_rejected():
    shown = context()
    first = assessment(shown)
    assert check(shown, findings=[first, first]).reasons == ("duplicate_identity",)
    invalid = assessment(shown, property_key="unknown.property")
    result = check(shown, findings=[first, invalid])
    assert result.status == "rejected" and result.assessments == ()


def test_abstention_does_not_imply_a_preference_or_choose_global_scope():
    shown = context(text="When is my exam?")
    result = check(shown, findings=[{"kind": "abstain", "reason": "not_preference"}])
    assert result.status == "attributed_proposals_only" and result.assessments == ()
    assert len(result.abstentions) == 1 and not result.automatic_activation_allowed


@pytest.mark.parametrize("change", ["foreign", "duplicate_target", "duplicate_identity", "unknown_scope", "unknown_property", "duplicate_scope", "unresolved_scope", "oversized", "too_many_properties"])
def test_input_catalog_and_snapshot_bounds_are_backend_owned(change):
    raw = context().model_dump(mode="json")
    if change == "foreign":
        raw["targets"][0]["tenant_id"] = str(uuid.UUID(int=99))
    elif change == "duplicate_target":
        raw["targets"].append(deepcopy(raw["targets"][0]))
    elif change == "duplicate_identity":
        target = {**raw["targets"][0], "ref": "t1", "memory_id": str(uuid.UUID(int=99))}
        raw["targets"].append(target)
    elif change == "duplicate_scope":
        raw["scopes"].append({**raw["scopes"][0], "ref": "also_global"})
    elif change == "unresolved_scope":
        raw["scopes"][0]["applicability"] = {"kind": "unresolved"}
    elif change == "unknown_scope":
        raw["targets"][0]["scope_ref"] = "unknown"
    elif change == "unknown_property":
        raw["targets"][0]["property_key"] = "unknown"
    elif change == "oversized":
        raw["turns"] = [{"role": "user", "content": "x" * 8000}, {"role": "user", "content": "x"}]
    else:
        raw["properties"] = [{"key": f"p{i}", "description": "Known property"} for i in range(17)]
    with pytest.raises(ValidationError):
        ContractContext.model_validate(raw)


def test_context_json_bound_counts_catalog_and_targets_not_just_turns():
    raw = context().model_dump(mode="json")
    raw["properties"] = [{"key": f"p{i}", "description": "x" * 400} for i in range(16)]
    raw["scopes"] = [{"ref": f"s{i}", "description": "x" * 400,
        "applicability": {"kind": "context", "namespace": "project", "key": str(i)}} for i in range(32)]
    raw["targets"] = [{**raw["targets"][0], "ref": f"t{i}", "memory_id": str(uuid.UUID(int=100+i)),
        "property_key": "p0", "scope_ref": f"s{i}", "value": "x" * 256} for i in range(32)]
    with pytest.raises(ValidationError, match="input limit exceeded"):
        ContractContext.model_validate(raw)


def test_output_findings_are_bounded():
    assert check(findings=[{"kind": "abstain", "reason": "not_preference"}] * 17).reasons == ("invalid_reply_shape",)


def test_protocol_and_contract_are_frozen_without_authorizing_execution():
    root = Path(__file__).resolve().parents[2]
    folder = root / "benchmarks/internal/protocols"
    protocol = json.loads((folder / "preference-semantic-admission-v1.json").read_text(encoding="utf-8"))
    freeze = json.loads((folder / "preference-semantic-admission-v1.freeze.json").read_text(encoding="utf-8"))
    assert protocol["protocol_id"] == freeze["protocol_id"] == "preference-semantic-admission-v1"
    for flag in ("provider_execution_approved", "holdout_execution_approved", "production_wiring_allowed", "automatic_activation_allowed"):
        assert protocol[flag] is False
    assert protocol["minimum_development_cases_total"] == (
        len(protocol["languages"]) * len(protocol["families"]) * protocol["minimum_development_cases_per_language_family"]
    ) == 360
    approved_paths = {
        "benchmarks/internal/preference_semantic_contract.py",
        "benchmarks/internal/protocols/preference-semantic-admission-v1.json",
        "docs/preference_semantic_admission_contract.md",
    }
    assert set(freeze["sha256"]) == approved_paths
    assert freeze["hash_encoding"] == "utf-8-lf"
    for path, expected_hash in freeze["sha256"].items():
        # Only these public protocol assets are read; never any dataset/holdout.
        canonical_text = (root / path).read_text(encoding="utf-8").encode("utf-8")
        assert hashlib.sha256(canonical_text).hexdigest() == expected_hash
