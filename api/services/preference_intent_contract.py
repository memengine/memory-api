"""Runtime intent contract and pure evidence binding.

This module does not classify text or write data. Semantics are model judgments,
not proof or authority. Its output must be rechecked by a transactional worker
before the existing writers can apply it. Queue use is default-off and tenant-gated.
"""
from __future__ import annotations

import uuid
from copy import deepcopy
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from api.schemas.memory_schemas import ExtractedMemory
from api.schemas.preferences import PreferenceClaimRequest, PropertyKey
from api.services.extraction_service import ExtractionService
from api.services.preference_choice_service import (
    PreferenceChoiceRequest,
    bind_choice_operations,
)
from api.services.preference_claim_service import declared_preferences
from api.services.preference_snapshot_service import (
    PreferenceSnapshot,
    validate_snapshot_coverage,
)


class _Closed(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class PreferenceIntent(_Closed):
    action: Literal["assert", "set_current", "withhold", "reaffirm", "tentative", "contextual", "abstain"]
    property_key: PropertyKey | None
    scope_ref: str | None = Field(pattern=r"^[a-z][a-z0-9_-]*$", max_length=64)
    value: str | None = Field(min_length=1, max_length=256)
    turn_index: int = Field(strict=True, ge=0, le=63)
    quote: str = Field(min_length=1, max_length=2000)

    @model_validator(mode="after")
    def represented_intent(self):
        if self.quote != self.quote.strip() or (self.value is not None and self.value not in self.quote):
            raise ValueError("intent requires verbatim evidence")
        if (self.property_key is None) != (self.scope_ref is None):
            raise ValueError("property and scope must be supplied together")
        if self.action != "abstain" and self.property_key is None:
            raise ValueError("represented intent requires an offered identity")
        if self.action in {"assert", "set_current"} and self.value is None:
            raise ValueError("current preference requires a value")
        if self.action in {"withhold", "reaffirm", "abstain"} and self.value is not None:
            raise ValueError("value-free intent must not supply a value")
        return self


class NonPreferenceFinding(_Closed):
    content: str = Field(min_length=1, max_length=2000)
    category: Literal["fact", "goal", "procedure", "relationship", "expertise"]
    turn_index: int = Field(strict=True, ge=0, le=63)
    quote: str = Field(min_length=1, max_length=2000)

    @model_validator(mode="after")
    def verbatim_finding(self):
        if self.quote != self.quote.strip() or self.content not in self.quote:
            raise ValueError("fact requires verbatim evidence")
        return self


class PreferenceIntentReply(_Closed):
    preferences: tuple[PreferenceIntent, ...] = Field(max_length=16)
    nonpreferences: tuple[NonPreferenceFinding, ...] = Field(max_length=16)

    @model_validator(mode="after")
    def bounded_disjoint_batch(self):
        if len(self.preferences) + len(self.nonpreferences) > 16:
            raise ValueError("combined finding limit exceeded")
        identities = [(p.property_key, p.scope_ref) for p in self.preferences if p.property_key is not None]
        if len(set(identities)) != len(identities):
            raise ValueError("one preference intent per identity")
        facts = [(f.turn_index, f.content) for f in self.nonpreferences]
        if len(set(facts)) != len(facts):
            raise ValueError("duplicate fact finding")
        for fact in self.nonpreferences:
            if any(fact.turn_index == p.turn_index and (fact.content in p.quote or p.quote in fact.content)
                   for p in self.preferences):
                raise ValueError("preference text cannot also enter the fact lane")
        return self


def candidate_generation_schema_v2() -> dict[str, Any]:
    """Provider-only structural constraints; persisted parser/evidence gates stay v1.

    All branches derive their common fields/bounds from the canonical model.
    This does not prove that a user expressed the proposed operation.
    """
    schema = PreferenceIntentReply.model_json_schema()
    base = schema["$defs"]["PreferenceIntent"]
    branches = []
    for actions, value_type, identity_type in (
        (["assert", "set_current"], "string", "string"),
        (["withhold", "reaffirm"], "null", "string"),
        (["tentative", "contextual"], None, "string"),
        (["abstain"], "null", "string"),
        (["abstain"], "null", "null"),
    ):
        branch = deepcopy(base)
        properties = branch["properties"]
        properties["action"]["enum"] = actions
        for name, required_type in (("value", value_type), ("property_key", identity_type),
                                    ("scope_ref", identity_type)):
            if required_type is not None:
                properties[name] = next(deepcopy(option) for option in properties[name]["anyOf"]
                                        if option["type"] == required_type)
        branches.append(branch)
    schema["$defs"]["PreferenceIntent"] = {"anyOf": branches}
    return schema


@dataclass(frozen=True, slots=True)
class BoundPreferenceIntentBatch:
    """Preparation, NOT write permission. Never serialize this as public input."""
    snapshot: PreferenceSnapshot
    source_conversation_id: uuid.UUID
    declarations: tuple[ExtractedMemory, ...]
    choices: tuple[ExtractedMemory, ...]
    pending_preferences: tuple[ExtractedMemory, ...]
    nonpreferences: tuple[ExtractedMemory, ...]
    question_reviews: tuple[dict[str, Any], ...] = ()


def _verified_memory(*, content, category, turn_index, quote, messages):
    memory = ExtractedMemory(content=content, category=category, importance_score=5,
        confidence=0.5, expiry="permanent", reasoning="Runtime intent preparation; semantics not independently attested.")
    evidence, reason = ExtractionService._validate_user_evidence(memory, messages,
        [turn_index], "direct_user_statement", evidence_spans=[{"turn_index": turn_index, "quote": quote}])
    if reason:
        raise ValueError(f"intent evidence rejected: {reason}")
    memory.validated_evidence = evidence
    return memory


def bind_preference_intents(reply, *, snapshot, messages, source_conversation_id, review_first=False):
    """Validate exact evidence and bind only backend-offered property/scope pairs.

    No SQL, providers, regex intent detection, or public contract expansion.
    The eventual worker must recheck these snapshot versions, including absence,
    under the existing ledger locks. A model cannot confer write authority.
    """
    reply = PreferenceIntentReply.model_validate(reply)
    validate_snapshot_coverage(snapshot)
    conversation_id = uuid.UUID(str(source_conversation_id))
    declarations, choices, pending, question_reviews = [], [], [], []
    for intent in reply.preferences:
        identity = next((i for i in snapshot.identities
            if (i.property_key, i.scope_ref) == (intent.property_key, intent.scope_ref)), None)
        if intent.property_key is not None and identity is None:
            raise ValueError("intent identity was not offered")
        if review_first and intent.action in {"assert", "set_current"}:
            # V2 never grants value-write permission from a model label or a
            # language-specific question heuristic. The same source/span and
            # identity checks bind every proposal to explicit confirmation.
            from api.services.preference_confirmation_service import prepare_question_review
            question_reviews.append(prepare_question_review(intent, snapshot=snapshot,
                messages=messages, source_conversation_id=conversation_id))
            continue  # Review evidence never becomes a declaration or value-free choice.
        memory = _verified_memory(content=intent.quote, category="preference",
            turn_index=intent.turn_index, quote=intent.quote, messages=messages)
        if intent.action in {"withhold", "reaffirm"}:
            if identity.target is None:
                pending.append(memory)  # Never invent a value to target an absent preference.
            else:
                choices.append(PreferenceChoiceRequest(action=intent.action, property_key=intent.property_key,
                    scope_ref=intent.scope_ref, turn_index=intent.turn_index, quote=intent.quote))
        elif intent.action in {"assert", "set_current"}:
            scope = next(s for s in snapshot.catalog.scopes if s.ref == intent.scope_ref)
            replacement = intent.action == "set_current" and identity.target is not None
            declarations.append(PreferenceClaimRequest(property_key=intent.property_key, value=intent.value,
                applicability=scope.applicability, turn_index=intent.turn_index, quote=intent.quote,
                commitment="replacement" if replacement else "asserted",
                replaces_memory_id=str(identity.target.memory_id) if replacement else None,
                replaces_version=identity.target.memory_version if replacement else None))
        else:
            pending.append(memory)  # Tentative/contextual/abstained text is not a current default.
    return BoundPreferenceIntentBatch(snapshot=snapshot, source_conversation_id=conversation_id,
        declarations=tuple(declared_preferences(declarations, messages)) if declarations else (),
        choices=tuple(bind_choice_operations(choices, snapshot=snapshot, messages=messages,
            source_conversation_id=conversation_id)) if choices else (),
        pending_preferences=tuple(pending),
        nonpreferences=tuple(_verified_memory(content=f.content, category=f.category,
            turn_index=f.turn_index, quote=f.quote, messages=messages) for f in reply.nonpreferences),
        question_reviews=tuple(question_reviews))
