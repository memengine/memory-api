"""Offline binding checks only; supplied interpretations are not model predictions."""
import hashlib
import json
import uuid
from copy import deepcopy
from pathlib import Path

import pytest
from pydantic import ValidationError

from benchmarks.internal import preference_semantic_binding_v2 as v2
from benchmarks.internal import preference_semantic_contract as v1
from tests.unit.test_preference_semantic_contract import changed, context


def snapshot(ctx=None, *, coverage=None):
    ctx = ctx or context()
    if coverage is None:
        coverage = [{"property_key": prop.key, "scope_ref": scope.ref} for prop in ctx.properties for scope in ctx.scopes]
    return v2.BindingSnapshot(context=ctx, covered_identities=coverage)


def interpretation(shown, **changes):
    return {"kind": "assessment", "property_key": "examples.language", "scope_ref": "global",
        "stance": "current_choice_unresolved", "evidence": {"turn_index": 0, "quote": shown.context.turns[0].content},
        "value": None, **changes}


def check(shown=None, *, current=None, findings=None, **changes):
    shown = shown or snapshot()
    return v2.validate_reply({"findings": findings or [interpretation(shown, **changes)]},
        shown=shown, current=current or shown)


def unrelated_context():
    raw = context(text="Python should be my default language.").model_dump(mode="json")
    raw["properties"].append({"key": "answer.detail", "description": "Preferred answer length."})
    raw["targets"][0].update(ref="t_other", property_key="answer.detail", value="brief")
    return v1.ContractContext.model_validate(raw)


def test_adoption_ignores_unrelated_target_and_binds_verified_absence():
    shown = snapshot(unrelated_context())
    before = shown.model_dump()
    result = check(shown, stance="asserted", value={"turn_index": 0, "quote": "Python"})
    assert result.status == "attributed_proposals_only"
    assert result.assessments[0].target_ref is None
    assert result.assessments[0].property_key == "examples.language"
    assert not result.automatic_activation_allowed and shown.model_dump() == before


def test_existing_exact_identity_is_bound_by_backend():
    shown = snapshot(context(text="Replace my default with Python."))
    result = check(shown, stance="replacement", value={"turn_index": 0, "quote": "Python"})
    assert result.status == "attributed_proposals_only"
    assert result.assessments[0].target_ref == "t0"
    assert result.assessments[0].stance == "replacement" and not result.automatic_activation_allowed


def test_project_binding_does_not_use_global_target():
    shown = snapshot(context(text="Use Python for Release only."))
    result = check(shown, stance="asserted", scope_ref="release", value={"turn_index": 0, "quote": "Python"})
    assert result.status == "attributed_proposals_only" and result.assessments[0].target_ref is None
    for stance in ("replacement", "current_choice_unresolved", "reaffirmed"):
        assert check(shown, stance=stance, scope_ref="release", value={"turn_index": 0, "quote": "Python"}).reasons == ("existing_target_required",)


def test_project_target_wins_exact_binding_without_archiving_global():
    raw = context(text="Use Python for Release only.").model_dump(mode="json")
    raw["targets"].append({**raw["targets"][0], "ref": "t_project", "scope_ref": "release", "memory_id": str(uuid.UUID(int=30))})
    shown = snapshot(v1.ContractContext.model_validate(raw))
    before = shown.model_dump()
    result = check(shown, stance="asserted", scope_ref="release", value={"turn_index": 0, "quote": "Python"})
    assert result.status == "attributed_proposals_only" and result.assessments[0].target_ref == "t_project"
    assert result.assessments[0].stance == "asserted"  # Never upgraded to replacement.
    assert shown.model_dump() == before and not result.automatic_activation_allowed


def test_unresolved_value_free_interpretation_stays_value_free():
    result = check()
    assert result.status == "attributed_proposals_only"
    assert result.assessments[0].target_ref == "t0" and result.assessments[0].value is None
    assert result.assessments[0].stance == "current_choice_unresolved"
    assert not result.automatic_activation_allowed


@pytest.mark.parametrize("extra", [{"target_ref": None}, {"target_ref": "t_other"}, {"memory_id": str(uuid.UUID(int=3))},
    {"authority": 100}, {"confidence": 0.99}, {"covered_identities": []}, {"automatic_activation_allowed": True}])
def test_model_cannot_supply_binding_or_governance_fields(extra):
    result = check(findings=[{**interpretation(snapshot()), **extra}])
    assert result.reasons == ("invalid_reply_shape",) and not result.assessments


def test_v1_wrong_target_reply_is_not_silently_repaired():
    shown = snapshot(unrelated_context())
    v1_reply = {**interpretation(shown, stance="asserted", value={"turn_index": 0, "quote": "Python"}), "target_ref": "t_other"}
    before = deepcopy(v1_reply)
    assert check(shown, findings=[v1_reply]).reasons == ("invalid_reply_shape",)
    assert v1_reply == before


@pytest.mark.parametrize("stance", ["replacement", "current_choice_unresolved", "reaffirmed"])
def test_existing_choice_stance_cannot_be_bound_to_absence(stance):
    shown = snapshot(unrelated_context())
    result = check(shown, stance=stance, value={"turn_index": 0, "quote": "Python"})
    assert result.reasons == ("existing_target_required",) and result.assessments == ()


@pytest.mark.parametrize("coverage", [[], [{"property_key": "examples.language", "scope_ref": "global"}]])
def test_partial_coverage_never_proves_absence_or_admits_a_reply(coverage):
    full = snapshot()
    partial = snapshot(full.context, coverage=coverage)
    assert v2.model_input(partial) is None
    assert check(partial).reasons == ("binding_coverage_incomplete",)
    assert check(full, current=partial).reasons == ("binding_coverage_incomplete",)
    # Existing targets do not make an otherwise partial snapshot complete.
    assert partial.context.targets and not v2.complete(partial)


@pytest.mark.parametrize("change", ["duplicate", "unknown_property", "unknown_scope", "overflow"])
def test_coverage_is_bounded_unique_and_backend_catalog_owned(change):
    shown = snapshot()
    raw = shown.model_dump(mode="json")
    if change == "duplicate":
        raw["covered_identities"].append(raw["covered_identities"][0])
    elif change == "unknown_property":
        raw["covered_identities"][0]["property_key"] = "foreign.property"
    elif change == "unknown_scope":
        raw["covered_identities"][0]["scope_ref"] = "unknown"
    else:
        raw["covered_identities"] = raw["covered_identities"][:1] * 513
    with pytest.raises(ValidationError):
        v2.BindingSnapshot.model_validate(raw)


def test_maximum_offered_identity_coverage_is_bounded_and_bindable():
    raw = context(text="Use Python.").model_dump(mode="json")
    raw["properties"] = [{"key": f"p{i}", "description": "Known property."} for i in range(16)]
    raw["scopes"] = [{"ref": f"s{i}", "description": "Known project.",
        "applicability": {"kind": "context", "namespace": "project", "key": str(i)}} for i in range(32)]
    raw["targets"] = []
    shown = snapshot(v1.ContractContext.model_validate(raw))
    assert len(shown.covered_identities) == 512 and v2.complete(shown)
    assert v2.model_input(shown) is not None
    result = check(shown, property_key="p0", scope_ref="s0", stance="asserted", value={"turn_index": 0, "quote": "Python"})
    assert result.status == "attributed_proposals_only" and result.assessments[0].target_ref is None
    assert not result.automatic_activation_allowed  # Not a load/capacity measurement.


@pytest.mark.parametrize("change", ["foreign_tenant", "foreign_profile", "duplicate_identity", "duplicate_memory", "target_overflow"])
def test_invalid_owned_target_context_cannot_be_hidden_in_nested_model_copy(change):
    ctx = context()
    if change in {"foreign_tenant", "foreign_profile"}:
        field = "tenant_id" if change == "foreign_tenant" else "profile_id"
        targets = (ctx.targets[0].model_copy(update={field: uuid.UUID(int=99)}),)
    elif change == "duplicate_identity":
        targets = (*ctx.targets, ctx.targets[0].model_copy(update={"ref": "t1", "memory_id": uuid.UUID(int=30)}))
    elif change == "duplicate_memory":
        targets = (*ctx.targets, ctx.targets[0].model_copy(update={"ref": "t1", "scope_ref": "release"}))
    else:
        targets = ctx.targets * 33
    copied = ctx.model_copy(update={"targets": targets})
    with pytest.raises(ValidationError):
        snapshot(copied)
    # Also fail at the reply boundary if a caller bypasses wrapper construction.
    tampered = snapshot(ctx).model_copy(update={"context": copied})
    assert check(tampered).reasons == ("invalid_binding_snapshot",)


@pytest.mark.parametrize("change", ["appeared", "disappeared", "version", "authority", "value", "choice_state", "currentness", "memory_id"])
def test_matching_identity_cannot_be_rebound_to_fresh_changed_state(change):
    ctx = context(text="Replace my default with Python.")
    shown = snapshot(ctx)
    if change == "appeared":
        shown = snapshot(ctx.model_copy(update={"targets": ()}))
        fresh = snapshot(ctx)
    elif change == "disappeared":
        fresh = snapshot(ctx.model_copy(update={"targets": ()}))
    else:
        changes = {"version": {"version": "b" * 64}, "authority": {"authority": 100},
            "value": {"value": "Ruby"}, "choice_state": {"choice_state": "current_choice_unresolved"},
            "currentness": {"is_current": False}, "memory_id": {"memory_id": str(uuid.UUID(int=30))}}
        fresh = snapshot(changed(ctx, "targets", **changes[change]))
    result = check(shown, current=fresh, stance="asserted", value={"turn_index": 0, "quote": "Python"})
    assert result.reasons == ("binding_target_changed",) and result.assessments == ()


def test_unchanged_tombstone_is_not_treated_as_absence():
    shown = snapshot(changed(context(), "targets", is_current=False))
    assert check(shown).reasons == ("target_missing_stale_or_ineligible",)


def test_unrelated_target_change_does_not_rebind_or_stale_the_matching_identity():
    raw = unrelated_context().model_dump(mode="json")
    shown = snapshot(v1.ContractContext.model_validate(raw))
    raw["targets"][0].update(value="detailed", version="b" * 64)
    fresh = snapshot(v1.ContractContext.model_validate(raw))
    result = check(shown, current=fresh, stance="asserted", value={"turn_index": 0, "quote": "Python"})
    assert result.status == "attributed_proposals_only" and result.assessments[0].target_ref is None


def test_coverage_order_does_not_change_exact_binding():
    shown = snapshot()
    fresh = snapshot(shown.context, coverage=list(reversed(shown.covered_identities)))
    assert check(shown, current=fresh).assessments[0].target_ref == "t0"


@pytest.mark.parametrize("section,updates,reason", [
    ("principal", {"blocked": True}, "principal_changed_or_blocked"),
    ("properties", {"description": "Changed catalog"}, "catalog_changed"),
    ("scopes", {"description": "Changed scope"}, "catalog_changed"),
    ("turns", {"content": "Changed source"}, "source_changed"),
])
def test_principal_catalog_and_source_are_rechecked_before_binding(section, updates, reason):
    shown = snapshot()
    fresh = snapshot(changed(shown.context, section, **updates))
    assert check(shown, current=fresh).reasons == (reason,)


def test_changed_owner_with_consistent_new_owner_targets_is_rejected():
    shown = snapshot()
    raw = shown.context.model_dump(mode="json")
    raw["principal"]["profile_id"] = str(uuid.UUID(int=99))
    raw["targets"][0]["profile_id"] = raw["principal"]["profile_id"]
    fresh = snapshot(v1.ContractContext.model_validate(raw))
    assert check(shown, current=fresh).reasons == ("principal_changed_or_blocked",)


@pytest.mark.parametrize("stance", ["replacement", "current_choice_unresolved", "reaffirmed"])
def test_binding_does_not_bypass_existing_authority_restrictions(stance):
    shown = snapshot(changed(context(text="Python"), "targets", authority=100))
    result = check(shown, stance=stance, value={"turn_index": 0, "quote": "Python"} if stance == "replacement" else None)
    assert result.reasons == ("authority_insufficient",) and not result.automatic_activation_allowed


@pytest.mark.parametrize("stance", ["asserted", "tentative_interest", "historical", "rejected_alternative", "unclear"])
def test_non_mutating_stance_never_grants_permission_over_high_authority(stance):
    shown = snapshot(changed(context(text="Python"), "targets", authority=100))
    result = check(shown, stance=stance, value={"turn_index": 0, "quote": "Python"})
    assert result.status == "attributed_proposals_only"
    assert result.assessments[0].stance == stance and result.assessments[0].target_ref == "t0"
    assert not result.automatic_activation_allowed


@pytest.mark.parametrize("role,kind", [("tool", None), ("document", None), ("assistant", None), ("system", None),
    ("user", "tool_output"), ("user", "fetched_document"), ("user", "assistant_output")])
def test_binding_cannot_turn_ineligible_source_into_user_evidence(role, kind):
    shown = snapshot(context(role=role, source_kind=kind))
    assert v2.model_input(shown) is None
    assert check(shown).reasons == ("invalid_source_span_turn",)


@pytest.mark.parametrize("text", ["अभी तय नहीं किया कि कौन सा डिफ़ॉल्ट रखूँ।", "Abhi decide nahi kiya kaunsa default rakhna hai."])
def test_unicode_quotes_survive_binding_without_claiming_language_recognition(text):
    shown = snapshot(context(text=text))
    assert check(shown).assessments[0].evidence.quote == text


def test_binding_does_not_relax_unique_evidence_requirement():
    shown = snapshot(context(text="Not decided. Not decided."))
    assert check(shown, evidence={"turn_index": 0, "quote": "Not decided."}).reasons == ("quote_not_unique",)


@pytest.mark.parametrize("changes,reason", [
    ({"property_key": "unknown.property"}, "identity_not_offered"),
    ({"scope_ref": "unknown"}, "identity_not_offered"),
    ({"evidence": {"turn_index": 63, "quote": "Invented"}}, "invalid_source_span_turn"),
    ({"evidence": {"turn_index": 0, "quote": "Invented"}}, "quote_not_found"),
    ({"value": {"turn_index": 0, "quote": "Ruby"}}, "value_outside_evidence"),
    ({"value": {"turn_index": 1, "quote": "Python"}}, "value_outside_evidence"),
])
def test_identity_and_evidence_gates_are_delegated_without_relaxation(changes, reason):
    shown = snapshot(context(text="Use Python now."))
    assert check(shown, **changes).reasons == (reason,)


def test_batch_validation_is_all_or_nothing():
    shown = snapshot()
    first = interpretation(shown)
    assert check(shown, findings=[first, first]).reasons == ("duplicate_identity",)
    result = check(shown, findings=[first, {**first, "scope_ref": "unknown"}])
    assert result.status == "rejected" and not result.assessments and not result.abstentions
    assert check(shown, findings=[first, {"kind": "abstain", "reason": "insufficient_evidence"}]).status == "attributed_proposals_only"


def test_attribution_and_binding_do_not_prove_negation_or_stance():
    shown = snapshot(context(text="I do not want Python as my default."))
    result = check(shown, stance="replacement", value={"turn_index": 0, "quote": "Python"})
    assert result.status == "attributed_proposals_only"  # Deliberately wrong supplied semantics.
    assert not result.automatic_activation_allowed


def test_model_projection_omits_all_backend_identity_and_coverage_fields():
    shown = snapshot()
    source = v2.model_input(shown)
    assert source is not None
    assert source["existing_preferences"] == [{"property_key": "examples.language", "scope_ref": "global", "value": "C++", "choice_state": "current"}]
    assert "existing_targets" not in source
    encoded = json.dumps(source)
    for private in (str(shown.context.principal.tenant_id), str(shown.context.principal.profile_id),
                    str(shown.context.targets[0].memory_id), "t0", "a" * 64,
                    "authority", "target_ref", "covered_identities", "automatic_activation_allowed"):
        assert private not in encoded
    assert v2.model_input(snapshot(changed(context(), "principal", blocked=True))) is None
    assert v2.model_input(snapshot(context(text="Could you use Python, and when is my exam?"))) is not None


def test_schema_exposes_exactly_six_required_interpretation_fields():
    schema = v2.response_schema(snapshot())
    definition = schema["$defs"]["Interpretation"]
    assert definition["required"] == list(definition["properties"]) == ["kind", "property_key", "scope_ref", "stance", "evidence", "value"]
    assert definition["additionalProperties"] is False
    assert definition["properties"]["property_key"]["enum"] == ["examples.language"]
    assert definition["properties"]["scope_ref"]["enum"] == ["global", "release"]
    assert "target_ref" not in json.dumps(schema)


@pytest.mark.parametrize("change", ["no_value", "long_value", "no_findings", "too_many_findings", "bool_index", "unknown_stance"])
def test_interpretation_bounds_are_preserved(change):
    shown = snapshot(context(text="Python"))
    raw = interpretation(shown, stance="asserted", value={"turn_index": 0, "quote": "Python"})
    findings = [raw]
    if change == "no_value":
        raw["value"] = None
    elif change == "long_value":
        raw["value"]["quote"] = "x" * 257
    elif change == "no_findings":
        findings = []
    elif change == "too_many_findings":
        findings *= 17
    elif change == "bool_index":
        raw["evidence"]["turn_index"] = True
    else:
        raw["stance"] = "write_current"
    assert v2.validate_reply({"findings": findings}, shown=shown, current=shown).reasons == ("invalid_reply_shape",)


def test_frozen_v1_dependencies_and_new_v2_assets_match_separate_freeze():
    root = Path(__file__).resolve().parents[2]
    folder = root / "benchmarks/internal/protocols"
    freeze = json.loads((folder / "preference-semantic-binding-v2.freeze.json").read_text(encoding="utf-8"))
    previous = json.loads((folder / "preference-semantic-admission-v1.freeze.json").read_text(encoding="utf-8"))
    protocol = json.loads((folder / "preference-semantic-binding-v2.json").read_text(encoding="utf-8"))
    assert freeze["protocol_id"] == protocol["protocol_id"] == "preference-semantic-binding-v2"
    assert freeze["hash_encoding"] == "utf-8-lf"
    expected = {*previous["sha256"], "benchmarks/internal/preference_semantic_binding_v2.py",
        "benchmarks/internal/protocols/preference-semantic-binding-v2.json", "docs/preference_semantic_binding_v2.md"}
    assert set(freeze["sha256"]) == expected
    for path, expected_hash in freeze["sha256"].items():
        assert hashlib.sha256((root / path).read_text(encoding="utf-8").encode("utf-8")).hexdigest() == expected_hash
    assert {path: freeze["sha256"][path] for path in previous["sha256"]} == previous["sha256"]
    for flag in ("provider_execution_approved", "holdout_execution_approved", "production_wiring_allowed",
                 "automatic_activation_allowed", "sql_completeness_verified", "sql_concurrency_verified",
                 "semantic_accuracy_measured", "human_release_gate_met", "latency_or_load_sla_measured"):
        assert protocol[flag] is False


def test_production_modules_do_not_import_the_offline_v2_contract():
    root = Path(__file__).resolve().parents[2]
    for path in (root / "api").rglob("*.py"):
        assert "preference_semantic_binding_v2" not in path.read_text(encoding="utf-8")
