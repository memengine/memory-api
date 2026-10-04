"""Offline semantic proposals with backend-owned exact-identity binding.

No provider, SQL, write adapter or production registration. Completeness is a
trusted fixture/supplier assertion here, not proof that a database was queried.
"""
from __future__ import annotations

from copy import deepcopy
from typing import Annotated, Literal

from pydantic import Field, ValidationError, model_validator

from benchmarks.internal import preference_semantic_contract as v1


class Identity(v1.ClosedModel):
    property_key: v1.PropertyKey
    scope_ref: v1.Ref


class BindingSnapshot(v1.ClosedModel):
    context: v1.ContractContext
    # Explicit coverage includes ABSENT identities, not just returned targets.
    # 16 properties * 32 scopes; never an automatically inferred completeness flag.
    covered_identities: tuple[Identity, ...] = Field(default=(), max_length=512)

    @model_validator(mode="after")
    def validate_coverage(self):
        # Revalidate nested copies too; model_copy can bypass v1 construction.
        v1.ContractContext.model_validate(self.context.model_dump())
        pairs = {(item.property_key, item.scope_ref) for item in self.covered_identities}
        if len(pairs) != len(self.covered_identities):
            raise ValueError("duplicate coverage identity")
        if not pairs <= offered_identities(self.context):
            raise ValueError("coverage outside offered catalog")
        return self


def offered_identities(context: v1.ContractContext) -> set[tuple[str, str]]:
    return {(prop.key, scope.ref) for prop in context.properties for scope in context.scopes}


def complete(snapshot: BindingSnapshot) -> bool:
    return {(item.property_key, item.scope_ref) for item in snapshot.covered_identities} == offered_identities(snapshot.context)


class Interpretation(v1.ClosedModel):
    kind: Literal["assessment"]
    property_key: v1.PropertyKey
    scope_ref: v1.Ref
    stance: v1.Stance
    evidence: v1.EvidenceQuote
    value: v1.EvidenceQuote | None

    @model_validator(mode="after")
    def require_value(self):
        if self.stance in {"asserted", "replacement"} and self.value is None:
            raise ValueError("a current value proposal requires value evidence")
        if self.value is not None and len(self.value.quote) > 256:
            raise ValueError("value limit exceeded")
        return self


class SemanticReply(v1.ClosedModel):
    findings: tuple[Annotated[Interpretation | v1.Abstention, Field(discriminator="kind")], ...] = Field(min_length=1, max_length=16)


def model_input(snapshot: BindingSnapshot) -> dict | None:
    """Use the existing source pre-filter; never offer an incomplete binding view."""
    snapshot = BindingSnapshot.model_validate(snapshot.model_dump())
    if not complete(snapshot):
        return None
    source = v1.model_input(snapshot.context)
    if source is None:
        return None
    targets = source.pop("existing_targets")
    source["existing_preferences"] = [{key: value for key, value in target.items() if key != "ref"} for target in targets]
    return source


def response_schema(snapshot: BindingSnapshot) -> dict:
    """Standard JSON Schema only; no provider adaptation or wire serializer."""
    snapshot = BindingSnapshot.model_validate(snapshot.model_dump())
    schema = deepcopy(SemanticReply.model_json_schema())
    fields = schema["$defs"]["Interpretation"]["properties"]
    fields["property_key"]["enum"] = [item.key for item in snapshot.context.properties]
    fields["scope_ref"]["enum"] = [item.ref for item in snapshot.context.scopes]
    return schema


def validate_reply(raw: dict, *, shown: BindingSnapshot, current: BindingSnapshot) -> v1.ValidationResult:
    """Bind only an unchanged exact identity, then reuse the frozen v1 validator.

    Targets in the returned assessments are BACKEND-derived. No rejected v1
    response is repaired/coerced, and there is no activation or write operation.
    """
    def reject(reason):
        return v1.ValidationResult(status="rejected", reasons=(reason,))

    try:
        reply = SemanticReply.model_validate(raw)
    except ValidationError:
        return reject("invalid_reply_shape")
    try:
        shown = BindingSnapshot.model_validate(shown.model_dump())
        current = BindingSnapshot.model_validate(current.model_dump())
    except ValidationError:
        return reject("invalid_binding_snapshot")
    # These metadata guards precede binding; v1 rechecks them on the bound reply.
    before, fresh = shown.context, current.context
    if fresh.principal.blocked or before.principal != fresh.principal:
        return reject("principal_changed_or_blocked")
    if before.properties != fresh.properties or before.scopes != fresh.scopes:
        return reject("catalog_changed")
    if before.turns != fresh.turns:
        return reject("source_changed")
    if not complete(shown) or not complete(current):
        return reject("binding_coverage_incomplete")
    offered = offered_identities(before)
    old_targets = {(target.property_key, target.scope_ref): target for target in before.targets}
    fresh_targets = {(target.property_key, target.scope_ref): target for target in fresh.targets}
    bound = []
    for finding in reply.findings:
        if isinstance(finding, v1.Abstention):
            bound.append(finding.model_dump(mode="json"))
            continue
        identity = (finding.property_key, finding.scope_ref)
        if identity not in offered:
            return reject("identity_not_offered")
        target = old_targets.get(identity)
        # Compare presence as well as the entire trusted snapshot. Never rebind
        # to a winner created/changed after interpretation began.
        if fresh_targets.get(identity) != target:
            return reject("binding_target_changed")
        if target is None and finding.stance in {"replacement", "current_choice_unresolved", "reaffirmed"}:
            return reject("existing_target_required")
        bound.append({**finding.model_dump(mode="json"), "target_ref": target.ref if target else None})
    return v1.validate_reply({"findings": bound}, shown=before, current=fresh)
