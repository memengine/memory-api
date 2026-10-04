"""Offline intent and candidate routing, never a production mutation adapter.

V1/v2 remain frozen. Reuse their source types, complete snapshot wrapper and the
production span validator; do not repair or reinterpret their consumed replies.
"""
from __future__ import annotations

from copy import deepcopy
from typing import Annotated, Literal

from pydantic import Field, ValidationError, model_validator

from api.services.evidence_policy import eligible_user_turn_indexes
from api.services.extraction_service import ExtractionService
from benchmarks.internal import preference_semantic_binding_v2 as binding
from benchmarks.internal import preference_semantic_contract as source

Intent = Literal[
    "adopts_value", "explicit_change", "current_choice_unresolved",
    "tentative_interest", "reaffirms_current", "rejects_alternative", "historical", "unclear",
]
CandidateRoute = Literal[
    "new_candidate", "duplicate_candidate", "replacement_candidate",
    "withhold_candidate", "reaffirm_candidate", "review_required", "no_current_change",
]


class IntentFinding(source.ClosedModel):
    kind: Literal["assessment"]
    property_key: source.PropertyKey
    scope_ref: source.Ref
    intent: Intent
    evidence: source.EvidenceQuote
    value: source.EvidenceQuote | None

    @model_validator(mode="after")
    def require_value(self):
        if self.intent in {"adopts_value", "explicit_change"} and self.value is None:
            raise ValueError("adoption/change requires a quoted value")
        if self.value is not None and len(self.value.quote) > 256:
            raise ValueError("value limit exceeded")
        return self


class SemanticReply(source.ClosedModel):
    findings: tuple[Annotated[IntentFinding | source.Abstention, Field(discriminator="kind")], ...] = Field(
        min_length=1, max_length=16,
    )


class BoundProposal(IntentFinding):
    # Derived exclusively from the trusted snapshots, never accepted as input.
    target_ref: source.Ref | None
    target_version: source.Version | None
    candidate_route: CandidateRoute
    reason: Literal[
        "verified_absence", "same_current_value", "explicit_change", "current_choice_unresolved",
        "explicit_reaffirmation", "preference_conflict", "existing_target_required",
        "authority_insufficient", "current_value_mismatch", "not_a_current_adoption", "unclear_intent",
    ]


class ValidationResult(source.ClosedModel):
    status: Literal["rejected", "attributed_proposals_only"]
    reasons: tuple[str, ...] = ()
    proposals: tuple[BoundProposal, ...] = ()
    abstentions: tuple[source.Abstention, ...] = ()
    automatic_activation_allowed: Literal[False] = False


def model_input(snapshot: binding.BindingSnapshot) -> dict | None:
    return binding.model_input(snapshot)


def response_schema(snapshot: binding.BindingSnapshot) -> dict:
    """Standard JSON Schema only; no provider dialect or execution path."""
    snapshot = binding.BindingSnapshot.model_validate(snapshot.model_dump())
    schema = deepcopy(SemanticReply.model_json_schema())
    fields = schema["$defs"]["IntentFinding"]["properties"]
    fields["property_key"]["enum"] = [item.key for item in snapshot.context.properties]
    fields["scope_ref"]["enum"] = [item.ref for item in snapshot.context.scopes]
    return schema


def _candidate_route(finding: IntentFinding, target: source.TargetSnapshot | None) -> tuple[CandidateRoute, str]:
    """Pure policy preview, not writer permission or a semantic correctness check."""
    if finding.intent == "unclear":
        return "review_required", "unclear_intent"
    if finding.intent in {"tentative_interest", "rejects_alternative", "historical"}:
        return "no_current_change", "not_a_current_adoption"
    if target is None:
        if finding.intent == "adopts_value":
            return "new_candidate", "verified_absence"
        # An explicit correction stays a correction even when the target is
        # absent. Never silently relabel it as a first adoption.
        return "review_required", "existing_target_required"
    if finding.intent == "adopts_value":
        if target.choice_state == "current_choice_unresolved":
            return "review_required", "current_choice_unresolved"
        if finding.value.quote == target.value:
            return "duplicate_candidate", "same_current_value"
        return "review_required", "preference_conflict"
    # Match the existing application-assertion ceiling. Intent/evidence cannot
    # grant authority, clear a stronger hold, or replace a stronger preference.
    if target.authority > 20:
        return "review_required", "authority_insufficient"
    if finding.intent == "current_choice_unresolved":
        return "withhold_candidate", "current_choice_unresolved"
    if finding.intent == "reaffirms_current":
        if finding.value is not None and finding.value.quote != target.value:
            return "review_required", "current_value_mismatch"
        return (
            ("reaffirm_candidate", "explicit_reaffirmation")
            if target.choice_state == "current_choice_unresolved"
            else ("duplicate_candidate", "same_current_value")
        )
    if finding.value.quote == target.value:
        return (
            ("reaffirm_candidate", "explicit_reaffirmation")
            if target.choice_state == "current_choice_unresolved"
            else ("duplicate_candidate", "same_current_value")
        )
    return "replacement_candidate", "explicit_change"


def validate_reply(
    raw: dict, *, shown: binding.BindingSnapshot, current: binding.BindingSnapshot,
) -> ValidationResult:
    """All-or-nothing attribution, exact binding and candidate routing only.

    No SQL, provider, normalization, state writes or conversion to the public
    declaration schema. Supplied complete snapshots do not prove SQL isolation.
    """
    def reject(reason):
        return ValidationResult(status="rejected", reasons=(reason,))

    try:
        reply = SemanticReply.model_validate(raw)
    except ValidationError:
        return reject("invalid_reply_shape")
    try:
        shown = binding.BindingSnapshot.model_validate(shown.model_dump())
        current = binding.BindingSnapshot.model_validate(current.model_dump())
    except ValidationError:
        return reject("invalid_binding_snapshot")
    before, fresh = shown.context, current.context
    if fresh.principal.blocked or before.principal != fresh.principal:
        return reject("principal_changed_or_blocked")
    if before.properties != fresh.properties or before.scopes != fresh.scopes:
        return reject("catalog_changed")
    if before.turns != fresh.turns:
        return reject("source_changed")
    if not binding.complete(shown) or not binding.complete(current):
        return reject("binding_coverage_incomplete")
    offered = binding.offered_identities(before)
    old_targets = {(target.property_key, target.scope_ref): target for target in before.targets}
    fresh_targets = {(target.property_key, target.scope_ref): target for target in fresh.targets}
    messages = [turn.model_dump(exclude_none=True) for turn in fresh.turns]
    eligible = set(eligible_user_turn_indexes(messages))
    seen = set()
    proposals, abstentions = [], []
    for finding in reply.findings:
        if isinstance(finding, source.Abstention):
            abstentions.append(finding)
            continue
        identity = (finding.property_key, finding.scope_ref)
        if identity not in offered:
            return reject("identity_not_offered")
        if identity in seen:
            return reject("duplicate_identity")
        seen.add(identity)
        target = old_targets.get(identity)
        if fresh_targets.get(identity) != target:
            return reject("binding_target_changed")
        if target is not None and not target.is_current:
            return reject("target_missing_stale_or_ineligible")
        spans, reason = ExtractionService._validate_source_spans(
            [finding.evidence.model_dump()], messages=messages, eligible_indexes=eligible,
        )
        if reason or not spans:
            return reject(reason or "invalid_evidence")
        if finding.value is not None and (
            finding.value.turn_index != finding.evidence.turn_index
            or finding.value.quote not in finding.evidence.quote
        ):
            return reject("value_outside_evidence")
        route, reason = _candidate_route(finding, target)
        proposals.append(BoundProposal(
            **finding.model_dump(), target_ref=target.ref if target else None,
            target_version=target.version if target else None, candidate_route=route, reason=reason,
        ))
    return ValidationResult(status="attributed_proposals_only", proposals=tuple(proposals), abstentions=tuple(abstentions))
