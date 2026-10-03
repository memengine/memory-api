"""Offline proposals only. No provider, filesystem, database or write adapter.

Attribution and snapshot checks cannot prove the semantic labels are correct.
Nothing here is imported by the production ingestion or retrieval paths.
"""
from __future__ import annotations

import uuid
from copy import deepcopy
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from api.schemas.preferences import PreferenceApplicability, PropertyKey
from api.services.evidence_policy import eligible_user_turn_indexes
from api.services.extraction_service import ExtractionService

Ref = Annotated[str, StringConstraints(min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9_-]*$")]
Version = Annotated[str, StringConstraints(pattern=r"^[a-f0-9]{64}$")]
Quote = Annotated[str, StringConstraints(min_length=1, max_length=1000)]
Value = Annotated[str, StringConstraints(min_length=1, max_length=256)]
Stance = Literal[
    "asserted", "replacement", "current_choice_unresolved", "tentative_interest",
    "reaffirmed", "rejected_alternative", "historical", "unclear",
]


class ClosedModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Principal(ClosedModel):
    tenant_id: uuid.UUID
    profile_id: uuid.UUID
    blocked: bool = Field(default=False, strict=True)


class PropertyDescriptor(ClosedModel):
    key: PropertyKey
    description: str = Field(min_length=1, max_length=400)


class ScopeDescriptor(ClosedModel):
    ref: Ref
    applicability: PreferenceApplicability
    description: str = Field(min_length=1, max_length=400)

    @model_validator(mode="after")
    def known_scope(self):
        if self.applicability.kind == "unresolved":
            raise ValueError("only known backend scopes can be offered")
        return self


class SourceTurn(ClosedModel):
    role: Literal["user", "assistant", "system", "tool", "document"]
    content: str = Field(max_length=8000)
    source_kind: str | None = Field(default=None, max_length=64)
    turn_id: str | None = Field(default=None, min_length=1, max_length=128)


class TargetSnapshot(ClosedModel):
    ref: Ref
    memory_id: uuid.UUID
    tenant_id: uuid.UUID
    profile_id: uuid.UUID
    property_key: PropertyKey
    scope_ref: Ref
    value: Value
    version: Version
    authority: int = Field(ge=0, le=100, strict=True)
    # Supplied by the backend/test fixture, never inferred from provider text.
    is_current: bool = Field(strict=True)
    choice_state: Literal["current", "current_choice_unresolved"] = "current"


class ContractContext(ClosedModel):
    principal: Principal
    properties: tuple[PropertyDescriptor, ...] = Field(min_length=1, max_length=16)
    scopes: tuple[ScopeDescriptor, ...] = Field(min_length=1, max_length=32)
    targets: tuple[TargetSnapshot, ...] = Field(default=(), max_length=32)
    turns: tuple[SourceTurn, ...] = Field(min_length=1, max_length=64)

    @model_validator(mode="after")
    def bounded_unique_context(self):
        property_keys = {item.key for item in self.properties}
        scope_refs = {item.ref for item in self.scopes}
        scope_identities = {item.applicability.model_dump_json(exclude_none=True) for item in self.scopes}
        if len(property_keys) != len(self.properties) or len(scope_refs) != len(self.scopes):
            raise ValueError("duplicate property or scope reference")
        if len(scope_identities) != len(self.scopes):
            raise ValueError("duplicate scope identity")
        if len({item.ref for item in self.targets}) != len(self.targets):
            raise ValueError("duplicate target reference")
        if len({item.memory_id for item in self.targets}) != len(self.targets):
            raise ValueError("duplicate target memory")
        if len({(item.property_key, item.scope_ref) for item in self.targets}) != len(self.targets):
            raise ValueError("multiple winners for one identity")
        for target in self.targets:
            if target.property_key not in property_keys or target.scope_ref not in scope_refs:
                raise ValueError("target outside offered catalog")
            if (target.tenant_id, target.profile_id) != (self.principal.tenant_id, self.principal.profile_id):
                raise ValueError("foreign target in context")
        if sum(len(turn.content) for turn in self.turns) > 8000 or len(self.model_dump_json()) > 32000:
            raise ValueError("input limit exceeded; do not truncate qualifiers")
        return self


class EvidenceQuote(ClosedModel):
    turn_index: int = Field(ge=0, le=63, strict=True)
    quote: Quote


class Assessment(ClosedModel):
    kind: Literal["assessment"]
    property_key: PropertyKey
    scope_ref: Ref
    target_ref: Ref | None
    stance: Stance
    evidence: EvidenceQuote
    # A separate source span, not a whole sentence presented as a settled value.
    # Null is valid for uncertainty/reaffirmation without a named alternative.
    value: EvidenceQuote | None

    @model_validator(mode="after")
    def required_references(self):
        if self.stance in {"asserted", "replacement"} and self.value is None:
            raise ValueError("a current value proposal requires value evidence")
        if self.stance in {"replacement", "current_choice_unresolved", "reaffirmed"} and self.target_ref is None:
            raise ValueError("a current-choice proposal requires an existing target")
        if self.value is not None and len(self.value.quote) > 256:
            raise ValueError("value limit exceeded")
        return self


class Abstention(ClosedModel):
    kind: Literal["abstain"]
    reason: Literal["not_preference", "unknown_property", "unknown_scope", "ambiguous_reference", "insufficient_evidence"]


class SemanticReply(ClosedModel):
    findings: tuple[Annotated[Assessment | Abstention, Field(discriminator="kind")], ...] = Field(min_length=1, max_length=16)


class ValidationResult(ClosedModel):
    status: Literal["rejected", "attributed_proposals_only"]
    reasons: tuple[str, ...] = ()
    assessments: tuple[Assessment, ...] = ()
    abstentions: tuple[Abstention, ...] = ()
    automatic_activation_allowed: Literal[False] = False


def model_input(context: ContractContext) -> dict | None:
    """Build a bounded interpretation input; never send authority or owner IDs.

    This cheap filter only skips blocked principals and inputs with no eligible
    user turn. It does not guess intent from a phrase, question or language.
    """
    messages = [item.model_dump(exclude_none=True) for item in context.turns]
    if context.principal.blocked or not eligible_user_turn_indexes(messages):
        return None
    return {
        "properties": [item.model_dump() for item in context.properties],
        "scopes": [item.model_dump() for item in context.scopes],
        "existing_targets": [{
            "ref": item.ref, "property_key": item.property_key, "scope_ref": item.scope_ref,
            "value": item.value, "choice_state": item.choice_state,
        } for item in context.targets],
        "turns": [{"turn_index": index, "role": item.role, "content": item.content,
                   "source_kind": item.source_kind} for index, item in enumerate(context.turns)],
    }


def response_schema(context: ContractContext) -> dict:
    """Standard JSON Schema, not a certified provider-specific strict dialect."""
    schema = deepcopy(SemanticReply.model_json_schema())
    fields = schema["$defs"]["Assessment"]["properties"]
    fields["property_key"]["enum"] = [item.key for item in context.properties]
    fields["scope_ref"]["enum"] = [item.ref for item in context.scopes]
    fields["target_ref"] = {"anyOf": [
        {"type": "string", "enum": [item.ref for item in context.targets]}, {"type": "null"},
    ]} if context.targets else {"type": "null"}
    return schema


def validate_reply(raw: dict, *, shown: ContractContext, current: ContractContext) -> ValidationResult:
    """Check a supplied reply against shown and fresh, trusted offline snapshots.

    All-or-nothing validation; no partial update, state mutation or confidence gate.
    The caller supplies snapshots here. A production adapter would need real SQL
    rechecks and locks; these pure comparisons are not a concurrency guarantee.
    """
    from pydantic import ValidationError

    def reject(reason):
        return ValidationResult(status="rejected", reasons=(reason,))

    try:
        reply = SemanticReply.model_validate(raw)
    except ValidationError:
        return reject("invalid_reply_shape")
    if current.principal.blocked or shown.principal != current.principal:
        return reject("principal_changed_or_blocked")
    if shown.properties != current.properties or shown.scopes != current.scopes:
        return reject("catalog_changed")
    if shown.turns != current.turns:
        return reject("source_changed")
    properties = {item.key for item in shown.properties}
    scopes = {item.ref for item in shown.scopes}
    targets = {item.ref: item for item in shown.targets}
    fresh = {item.ref: item for item in current.targets}
    messages = [item.model_dump(exclude_none=True) for item in current.turns]
    eligible = set(eligible_user_turn_indexes(messages))
    identities = set()
    assessments, abstentions = [], []
    for finding in reply.findings:
        if isinstance(finding, Abstention):
            abstentions.append(finding)
            continue
        if finding.property_key not in properties or finding.scope_ref not in scopes:
            return reject("identity_not_offered")
        identity = (finding.property_key, finding.scope_ref)
        if identity in identities:
            return reject("duplicate_identity")
        identities.add(identity)
        target = targets.get(finding.target_ref)
        if finding.target_ref is not None:
            if target is None or fresh.get(finding.target_ref) != target or not target.is_current:
                return reject("target_missing_stale_or_ineligible")
            if (target.property_key, target.scope_ref) != identity:
                return reject("target_identity_mismatch")
            if finding.stance in {"replacement", "current_choice_unresolved", "reaffirmed"} and target.authority > 20:
                return reject("authority_insufficient")
        elif any((item.property_key, item.scope_ref) == identity for item in current.targets):
            return reject("existing_target_omitted")
        spans, reason = ExtractionService._validate_source_spans(
            [finding.evidence.model_dump()], messages=messages, eligible_indexes=eligible,
        )
        if reason or not spans:
            return reject(reason or "invalid_evidence")
        if finding.value is not None and (
            finding.value.turn_index != finding.evidence.turn_index or finding.value.quote not in finding.evidence.quote
        ):
            return reject("value_outside_evidence")
        assessments.append(finding)
    return ValidationResult(status="attributed_proposals_only", assessments=tuple(assessments), abstentions=tuple(abstentions))
