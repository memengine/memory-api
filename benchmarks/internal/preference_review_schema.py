"""Versioned human review records, not model replies or write authorization."""
from __future__ import annotations

from typing import Annotated, Literal

from pydantic import Field, StringConstraints, model_validator

from api.schemas.preferences import PropertyKey
from benchmarks.internal.preference_semantic_contract import (
    ClosedModel,
    EvidenceQuote,
    Ref,
    Stance,
    Version,
)

REVIEW_SCHEMA_VERSION = "preference-human-review-v1"
Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=1000)]
ReviewerIdentity = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=120)]
CaseId = Annotated[str, StringConstraints(pattern=r"^d[0-9]{4}$")]
NoFindingsReason = Literal[
    "not_preference", "untrusted_source", "ambiguous_reference", "unknown_property",
    "unknown_scope", "insufficient_evidence", "unrepresentable",
]


class HumanFinding(ClosedModel):
    property_key: PropertyKey | None
    scope_ref: Ref | None
    target_ref: Ref | None
    stance: Stance
    evidence: tuple[EvidenceQuote, ...] = Field(min_length=1, max_length=8)
    value: EvidenceQuote | None
    property_description: Text | None = None
    scope_description: Text | None = None
    value_description: Text | None = None

    @model_validator(mode="after")
    def describe_missing_bindings(self):
        if self.property_key is None and self.property_description is None:
            raise ValueError("unbound property requires property_description")
        if self.scope_ref is None and self.scope_description is None:
            raise ValueError("unbound scope requires scope_description")
        if self.stance in {"asserted", "replacement"} and self.value is None and self.value_description is None:
            raise ValueError("unquoted adopted value requires value_description, not an invented quote")
        return self


class ReviewAnnotation(ClosedModel):
    findings: tuple[HumanFinding, ...] = Field(max_length=16)
    no_findings_reason: NoFindingsReason | None
    representability: Literal["representable", "unrepresentable"]
    representability_reason: Text | None
    ambiguity: Literal["unambiguous", "ambiguous"]
    ambiguity_reason: Text | None
    language_quality: Literal["natural", "needs_revision"]
    language_notes: Text | None
    alternative_interpretations: tuple[Text, ...] = Field(default=(), max_length=8)
    notes: Text | None = None

    @model_validator(mode="after")
    def consistent_judgments(self):
        if bool(self.findings) != (self.no_findings_reason is None):
            raise ValueError("empty findings require a reason; nonempty findings forbid it")
        if (self.representability == "unrepresentable") != (self.representability_reason is not None):
            raise ValueError("only unrepresentable judgments require representability_reason")
        if self.no_findings_reason == "unrepresentable" and self.representability != "unrepresentable":
            raise ValueError("unrepresentable no-findings reason requires unrepresentable judgment")
        if (self.ambiguity == "ambiguous") != (self.ambiguity_reason is not None):
            raise ValueError("only ambiguous judgments require ambiguity_reason")
        if self.ambiguity == "ambiguous" and len(self.alternative_interpretations) < 2:
            raise ValueError("ambiguous judgments require at least two alternative interpretations")
        if self.ambiguity == "unambiguous" and self.alternative_interpretations:
            raise ValueError("unambiguous judgments forbid alternative interpretations")
        if len(set(self.alternative_interpretations)) != len(self.alternative_interpretations):
            raise ValueError("duplicate alternative interpretations")
        if (self.language_quality == "needs_revision") != (self.language_notes is not None):
            raise ValueError("only needs_revision judgments require language_notes")
        if len({finding.model_dump_json() for finding in self.findings}) != len(self.findings):
            raise ValueError("duplicate human finding")
        return self


class ReviewWorksheet(ClosedModel):
    schema_version: Literal["preference-human-review-v1"]
    pack_id: Literal["preference-semantic-development-v1"]
    inputs_sha256_utf8_lf: Version
    review_pass: Literal["a", "b"]
    reviewer_identity: ReviewerIdentity | None
    reviewer_is_human: bool | None = Field(strict=True)
    status: Literal["pending", "in_progress", "completed"]
    annotations: dict[CaseId, ReviewAnnotation | None] = Field(min_length=1, max_length=360)

    @model_validator(mode="after")
    def completion_state(self):
        filled = sum(annotation is not None for annotation in self.annotations.values())
        if self.status == "pending" and filled:
            raise ValueError("pending worksheet must have no filled annotations")
        if self.status == "in_progress" and not 0 < filled < len(self.annotations):
            raise ValueError("in_progress worksheet requires both filled and unfilled annotations")
        if self.status == "completed" and filled != len(self.annotations):
            raise ValueError("completed worksheet cannot contain unreviewed annotations")
        if self.status != "pending" and (self.reviewer_identity is None or self.reviewer_is_human is not True):
            raise ValueError("in_progress/completed require an identified, self-declared human reviewer")
        return self
