"""Opt-in declarations, not semantic inference or user-role attestation.

Kept independent of the backend package for standalone SDK installation.
Contract parity is checked against the backend in repository tests.
"""
from __future__ import annotations

import uuid
from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

PropertyKey = Annotated[str, StringConstraints(min_length=1, max_length=120, pattern=r"^[a-z][a-z0-9._-]*$")]


class PreferenceApplicability(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    kind: Literal["global", "context", "unresolved"]
    namespace: PropertyKey | None = None
    key: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=128)] | None = None

    @model_validator(mode="after")
    def validate_context(self):
        if (self.kind == "context") != (self.namespace is not None and self.key is not None):
            raise ValueError("context requires namespace and key; other kinds forbid them")
        if self.kind != "context" and (self.namespace is not None or self.key is not None):
            raise ValueError("only context applicability accepts namespace/key")
        return self


class PreferenceScopeRequest(BaseModel):
    """Read-time scope question; not permission to activate a value."""
    model_config = ConfigDict(extra="forbid", frozen=True)
    state: Literal["application_scope_required"] = "application_scope_required"
    property_keys: tuple[PropertyKey, ...] = Field(min_length=1, max_length=16)
    available_scopes: tuple[PreferenceApplicability, ...] = Field(min_length=1, max_length=32)

    @model_validator(mode="after")
    def known_unique_scopes(self):
        if len(set(self.property_keys)) != len(self.property_keys):
            raise ValueError("scope request keys must be unique")
        scopes = [scope.model_dump_json() for scope in self.available_scopes]
        if len(set(scopes)) != len(scopes) or any(scope.kind == "unresolved" for scope in self.available_scopes):
            raise ValueError("scope request requires unique known application scopes")
        return self


class PreferenceClaimRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    property_key: PropertyKey
    value: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=256)]
    applicability: PreferenceApplicability
    turn_index: int = Field(strict=True, ge=0, le=63)
    quote: str = Field(min_length=1, max_length=2000)
    commitment: Literal["asserted", "uncertain", "replacement"] = "asserted"
    # Tentative interest alone must not suspend an existing default.
    choice_state: Literal["candidate_only", "current_choice_unresolved"] = "candidate_only"
    replaces_memory_id: Annotated[str, StringConstraints(pattern=r"^[a-f0-9-]{36}$")] | None = None
    replaces_version: Annotated[str, StringConstraints(pattern=r"^[a-f0-9]{64}$")] | None = None

    @field_validator("replaces_memory_id")
    @classmethod
    def validate_target_id(cls, value):
        return str(uuid.UUID(value)) if value is not None else None

    @model_validator(mode="after")
    def validate_replacement(self):
        if self.value not in self.quote:
            raise ValueError("value must occur verbatim in the evidence quote")
        if self.quote != self.quote.strip():
            raise ValueError("quote must not have surrounding whitespace")
        references = self.replaces_memory_id is not None or self.replaces_version is not None
        if self.choice_state == "current_choice_unresolved" and self.commitment != "uncertain":
            raise ValueError("current_choice_unresolved requires uncertain commitment")
        if self.choice_state == "current_choice_unresolved" and self.applicability.kind == "unresolved":
            raise ValueError("withholding requires known applicability")
        if self.commitment == "replacement" or self.choice_state == "current_choice_unresolved":
            if self.replaces_memory_id is None or self.replaces_version is None:
                raise ValueError("replacement or unresolved current choice requires target memory id and version")
        elif references:
            raise ValueError("only replacement or unresolved current choice accepts a target")
        return self


class PreferenceReadDecision(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    property_key: PropertyKey
    applicability: PreferenceApplicability
    state: Literal["current_choice_unresolved"] = "current_choice_unresolved"


class PreferenceReadContext(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    applicability: PreferenceApplicability
    property_keys: tuple[PropertyKey, ...] = Field(min_length=1, max_length=16)

    @model_validator(mode="after")
    def validate_keys(self):
        if self.applicability.kind == "unresolved":
            raise ValueError("reads require known applicability")
        if len(set(self.property_keys)) != len(self.property_keys):
            raise ValueError("property_keys must be unique")
        return self
