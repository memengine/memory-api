"""Internal, value-free choices over trusted snapshots; no live classifier wiring.

The request supplies intent, not authority. The backend binds identity/version,
verifies user spans and rechecks the ledger in the existing writer transaction.
These contracts are deliberately not part of the public add-memory schema.
"""
from __future__ import annotations

import hashlib
import json
import uuid
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator
from sqlalchemy import select

from api.db.models import Memory, MemoryClaim, MemoryClaimRevision
from api.schemas.memory_schemas import ExtractedMemory
from api.schemas.preferences import PreferenceApplicability, PropertyKey
from api.services.extraction_service import ExtractionService
from api.services.preference_claim_service import (
    CHOICE_STATE_MARKER,
    preference_fingerprint,
)
from api.services.preference_snapshot_service import (
    PreferenceSnapshot,
    PreferenceSnapshotUnavailable,
    identity_binding_version,
    snapshot_target,
)

CHOICE_OPERATION_MARKER = "preference_choice_operation_v1"
Version = Annotated[str, StringConstraints(pattern=r"^[a-f0-9]{64}$")]


class PreferenceChoiceRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    action: Literal["withhold", "reaffirm"]
    property_key: PropertyKey
    scope_ref: str = Field(min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9_-]*$")
    turn_index: int = Field(strict=True, ge=0, le=63)
    quote: str = Field(min_length=1, max_length=2000)

    @model_validator(mode="after")
    def clean_quote(self):
        if self.quote != self.quote.strip():
            raise ValueError("quote must not have surrounding whitespace")
        return self


class PreferenceChoiceOperation(PreferenceChoiceRequest):
    """Backend-bound descriptor. No proposed value or caller authority field."""
    tenant_id: uuid.UUID
    proxy_user_id: uuid.UUID
    claim_id: uuid.UUID
    target_memory_id: uuid.UUID
    target_version: Version
    binding_version: Version
    applicability: PreferenceApplicability
    source_conversation_id: uuid.UUID

    @model_validator(mode="after")
    def known_scope(self):
        if self.applicability.kind == "unresolved":
            raise ValueError("choice operation requires known applicability")
        return self

    @property
    def fingerprint(self) -> str:
        return preference_fingerprint(self.property_key, self.applicability)

    @property
    def evidence_key(self) -> str:
        return hashlib.sha256(json.dumps(self.model_dump(mode="json"), sort_keys=True).encode()).hexdigest()


def bind_choice_operations(requests, *, snapshot: PreferenceSnapshot, messages, source_conversation_id):
    """Bounded preparation, entirely before SQL locks; attribution is not semantics."""
    if not 1 <= len(requests) <= 16:
        raise ValueError("choice operations require 1..16 requests")
    if snapshot.catalog.tenant_id != snapshot.tenant_id:
        raise ValueError("choice catalog tenant mismatch")
    result, seen = [], set()
    for raw in requests:
        request = PreferenceChoiceRequest.model_validate(raw)
        identity = next((item for item in snapshot.identities
            if (item.property_key, item.scope_ref) == (request.property_key, request.scope_ref)), None)
        scope = next((item for item in snapshot.catalog.scopes if item.ref == request.scope_ref), None)
        if (identity is None or scope is None or identity.claim_id is None or identity.target is None
            or request.property_key not in {item.key for item in snapshot.catalog.properties}
            or identity.fingerprint != preference_fingerprint(request.property_key, scope.applicability)):
            raise ValueError("choice target unavailable; no absence or fallback binding")
        if identity.fingerprint in seen:
            raise ValueError("one choice per identity per batch")
        seen.add(identity.fingerprint)
        operation = PreferenceChoiceOperation(
            **request.model_dump(), tenant_id=snapshot.tenant_id, proxy_user_id=snapshot.proxy_user_id,
            claim_id=identity.claim_id, target_memory_id=identity.target.memory_id,
            target_version=identity.target.memory_version, binding_version=identity.binding_version,
            applicability=scope.applicability, source_conversation_id=source_conversation_id,
        )
        memory = ExtractedMemory(content=request.quote, category="preference", importance_score=5,
            confidence=0.5, expiry="permanent", reasoning="Internal value-free choice; semantics not attested.")
        evidence, reason = ExtractionService._validate_user_evidence(
            memory, messages, [request.turn_index], "direct_user_statement",
            evidence_spans=[{"turn_index": request.turn_index, "quote": request.quote}],
        )
        if reason:
            raise ValueError(f"choice evidence rejected: {reason}")
        memory.validated_evidence = {**evidence, CHOICE_OPERATION_MARKER: operation.model_dump(mode="json"),
            "preference_fingerprint": operation.fingerprint}
        result.append(memory)
    return result


def lock_choice_target(session, operation: PreferenceChoiceOperation, *, user_id):
    """Ledger then target; no new reservation, provider work or transaction commit."""
    ledger = session.execute(select(MemoryClaim).where(
        MemoryClaim.id == operation.claim_id, MemoryClaim.tenant_id == operation.tenant_id,
        MemoryClaim.proxy_user_id == operation.proxy_user_id,
        MemoryClaim.claim_fingerprint == operation.fingerprint,
    ).with_for_update().execution_options(populate_existing=True)).scalar_one_or_none()
    if ledger is None:
        return None, None, "preference_target_stale"
    target = session.execute(select(Memory).where(
        Memory.id == ledger.active_memory_id, Memory.id == operation.target_memory_id,
        Memory.proxy_user_id == operation.proxy_user_id, Memory.user_id == uuid.UUID(str(user_id)),
    ).with_for_update().execution_options(populate_existing=True)).scalar_one_or_none()
    revision = session.execute(select(MemoryClaimRevision).where(
        MemoryClaimRevision.id == ledger.winning_revision_id, MemoryClaimRevision.claim_id == ledger.id,
        MemoryClaimRevision.memory_id == operation.target_memory_id,
    ).with_for_update().execution_options(populate_existing=True)).scalar_one_or_none()
    try:
        current = snapshot_target(ledger, target, revision, property_key=operation.property_key,
            applicability=operation.applicability)
    except PreferenceSnapshotUnavailable:
        return ledger, target, "preference_ledger_inconsistent"
    if current.authority > 20:
        return ledger, target, "preference_authority_insufficient"
    if not current.is_current:
        return ledger, target, "preference_target_stale"
    desired = "current_choice_unresolved" if operation.action == "withhold" else "current"
    marker = (target.metadata_json or {}).get(CHOICE_STATE_MARKER) or {}
    # Only the latest applied operation may deduplicate itself. This path never
    # writes, recreates a review, or restores a choice after another generation.
    if marker.get("operation_key") == operation.evidence_key and current.choice_state == desired:
        return ledger, target, "DEDUP"
    if (current.memory_version != operation.target_version
        or identity_binding_version(ledger, current) != operation.binding_version):
        return ledger, target, "preference_target_stale"
    if operation.action == "reaffirm" and current.choice_state != "current_choice_unresolved":
        return ledger, target, "preference_choice_not_held"
    return ledger, target, "WITHHOLD" if operation.action == "withhold" else "REAFFIRM"
