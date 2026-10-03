"""Bounded preference policy over the existing memory/claim transaction.

Property and applicability are application assertions, not linguistic proof.
They never inherit registered-writer authority. No classifier or vector search
chooses a replacement; an owned, version-bound exact identity does.
"""
from __future__ import annotations

import hashlib
import json
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from api.db.models import Memory, MemoryCategory, MemoryClaim, MemoryClaimRevision
from api.schemas.memory_schemas import ExtractedMemory
from api.schemas.preferences import PreferenceApplicability, PreferenceClaimRequest
from api.services.claim_ledger_service import (
    authority_priority,
    build_domain_claim_identity,
)
from api.services.extraction_service import ExtractionService
from api.services.source_review_service import target_version

PREFERENCE_MARKER = "explicit_preference_v1"
CHOICE_STATE_MARKER = "preference_choice_state"


def choice_evidence_key(declaration: PreferenceClaimRequest) -> str:
    return hashlib.sha256(json.dumps(
        declaration.model_dump(mode="json", exclude_none=True), sort_keys=True,
    ).encode()).hexdigest()


def held_choice(memory: Memory, ledger: MemoryClaim) -> bool:
    """A durable read barrier, independent of pending-review retention."""
    marker = (memory.metadata_json or {}).get(CHOICE_STATE_MARKER)
    declaration = preference_declaration((memory.metadata_json or {}).get("provenance"))
    return bool(
        isinstance(marker, dict) and marker.get("state") == "current_choice_unresolved"
        and declaration is not None
        and marker.get("fingerprint") == preference_identity(declaration).fingerprint == ledger.claim_fingerprint
        and ledger.active_memory_id == memory.id and ledger.active_value == declaration.value
        and str(memory.proxy_user_id) == str(ledger.proxy_user_id)
    )


def set_choice_state(memory: Memory, ledger: MemoryClaim, *, unresolved: bool, declaration=None):
    # A new backend nonce changes target_version on both hold and reaffirmation.
    # Old jobs/reviews cannot replay against a later decision on the same row.
    previous = dict((memory.metadata_json or {}).get(CHOICE_STATE_MARKER) or {})
    memory.metadata_json = {**dict(memory.metadata_json or {}), CHOICE_STATE_MARKER: {
        "state": "current_choice_unresolved" if unresolved else "current",
        "fingerprint": ledger.claim_fingerprint, "generation": str(uuid.uuid4()),
        "evidence_key": choice_evidence_key(declaration) if unresolved else previous.get("evidence_key"),
    }}
    ledger.status = "disputed" if unresolved else "active"
    ledger.updated_at = datetime.now(UTC)


def current_target(memory: Memory) -> bool:
    now = datetime.now(UTC)
    def utc(value):
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
    return not memory.is_archived and not any(
        getattr(memory, name, None) is not None and utc(getattr(memory, name)) <= now
        for name in ("expires_at", "effective_until")
    ) and not (getattr(memory, "effective_from", None) is not None and utc(memory.effective_from) > now)


async def preference_review_claim(session, candidate):
    """Acquire ledger before target, NOWAIT while the review row is held."""
    declaration = preference_declaration({"extraction_evidence":
        (candidate.metadata_json or {}).get("extraction_evidence") or {}})
    if declaration is None or declaration.choice_state != "current_choice_unresolved":
        return None
    claim = (await session.execute(select(MemoryClaim).where(
        MemoryClaim.tenant_id == candidate.tenant_id,
        MemoryClaim.proxy_user_id == candidate.proxy_user_id,
        MemoryClaim.claim_fingerprint == preference_identity(declaration).fingerprint,
    ).with_for_update(nowait=True).execution_options(populate_existing=True))).scalar_one_or_none()
    if claim is None:
        from api.errors import APIError
        raise APIError(status_code=409, code="REV_409", error="source_review_stale_or_closed")
    return claim


def reaffirm_review_choice(candidate, memory: Memory, ledger: MemoryClaim) -> bool:
    declaration = preference_declaration({"extraction_evidence":
        (candidate.metadata_json or {}).get("extraction_evidence") or {}})
    marker = (memory.metadata_json or {}).get(CHOICE_STATE_MARKER) or {}
    if not held_choice(memory, ledger) or ledger.status != "disputed" or not current_target(memory):
        return False
    if int(ledger.authority_priority) > 20 or authority_priority((memory.metadata_json or {}).get("provenance"), "preference") > 20:
        return False
    if declaration is None or marker.get("evidence_key") != choice_evidence_key(declaration):
        return False
    set_choice_state(memory, ledger, unresolved=False)
    return True


def preference_declaration(provenance: dict[str, Any] | None) -> PreferenceClaimRequest | None:
    raw = ((provenance or {}).get("extraction_evidence") or {}).get(PREFERENCE_MARKER)
    return PreferenceClaimRequest.model_validate(raw) if raw is not None else None


def preference_identity(claim: PreferenceClaimRequest):
    return build_domain_claim_identity(
        domain=PREFERENCE_MARKER, field=claim.property_key, category="preference",
        value=claim.value, scope=claim.applicability.model_dump(exclude_none=True),
    )


def preference_fingerprint(property_key: str, applicability: PreferenceApplicability) -> str:
    return build_domain_claim_identity(
        domain=PREFERENCE_MARKER, field=property_key, category="preference", value="",
        scope=applicability.model_dump(exclude_none=True),
    ).fingerprint


def declared_preferences(claims, messages: list[dict[str, Any]]) -> list[ExtractedMemory]:
    if not 1 <= len(claims) <= 16:
        raise ValueError("explicit preferences require 1..16 claims")
    result = []
    identities = set()
    for raw in claims:
        claim = PreferenceClaimRequest.model_validate(raw)
        fingerprint = preference_identity(claim).fingerprint
        if fingerprint in identities:
            raise ValueError("one declaration per property/applicability is allowed per job")
        identities.add(fingerprint)
        memory = ExtractedMemory(
            content=claim.quote, category="preference", importance_score=5,
            confidence=0.5, expiry="permanent", reasoning="Application-declared preference; semantics not model-attested.",
        )
        evidence, reason = ExtractionService._validate_user_evidence(
            memory, messages, [claim.turn_index], "direct_user_statement",
            evidence_spans=[{"turn_index": claim.turn_index, "quote": claim.quote}],
        )
        if reason:
            raise ValueError(f"explicit preference evidence rejected: {reason}")
        memory.validated_evidence = {
            **evidence, "citation_mode": "application_declared",
            PREFERENCE_MARKER: claim.model_dump(mode="json", exclude_none=True),
            "preference_fingerprint": fingerprint,
        }
        result.append(memory)
    return result


def reserve_claim(session, *, tenant_id: str, proxy_user_id: str, declaration: PreferenceClaimRequest):
    identity = preference_identity(declaration)
    # ON CONFLICT covers the first-writer race; row locking serializes subsequent
    # target/version decisions. All calls are in the memory/outbox transaction.
    session.execute(insert(MemoryClaim).values(
        id=uuid.uuid4(), tenant_id=uuid.UUID(tenant_id), proxy_user_id=uuid.UUID(proxy_user_id),
        category=MemoryCategory.preference, claim_fingerprint=identity.fingerprint,
        subject_key=identity.subject_key, predicate_key=identity.predicate_key,
        scope=identity.scope, status="disputed", authority_priority=20, confidence_score=0,
    ).on_conflict_do_nothing(constraint="uq_memory_claims_tenant_user_fingerprint"))
    return session.execute(select(MemoryClaim).where(
        MemoryClaim.tenant_id == uuid.UUID(tenant_id),
        MemoryClaim.proxy_user_id == uuid.UUID(proxy_user_id),
        MemoryClaim.claim_fingerprint == identity.fingerprint,
    ).with_for_update().execution_options(populate_existing=True)).scalar_one()


def transition_reason(declaration: PreferenceClaimRequest, ledger: MemoryClaim, target: Memory | None) -> str:
    holds_current = declaration.choice_state == "current_choice_unresolved"
    if declaration.applicability.kind == "unresolved" or (declaration.commitment == "uncertain" and not holds_current):
        return "preference_requires_review"
    if ledger.active_memory_id is None:
        return "preference_target_stale" if declaration.commitment == "replacement" or holds_current else "NEW"
    if target is None or str(target.id) != str(ledger.active_memory_id):
        return "preference_target_stale"
    stored = preference_declaration((target.metadata_json or {}).get("provenance"))
    if stored is None or preference_identity(stored).fingerprint != preference_identity(declaration).fingerprint:
        return "preference_identity_mismatch"
    holding = held_choice(target, ledger)
    if ledger.status not in {"active", "archived"} and not (ledger.status == "disputed" and holding):
        return "preference_ledger_inconsistent"
    if ledger.active_value != stored.value:
        return "preference_ledger_inconsistent"
    if ledger.status == "archived" and not target.is_archived:
        return "preference_ledger_inconsistent"
    if declaration.commitment == "replacement" or holds_current:
        if declaration.replaces_memory_id != str(target.id) or declaration.replaces_version != target_version(target):
            return "preference_target_stale"
        # The caller cannot upgrade its application assertion by citing a writer
        # event, or overwrite a future higher-authority preference.
        if int(ledger.authority_priority) > 20 or authority_priority((target.metadata_json or {}).get("provenance"), "preference") > 20:
            return "preference_authority_insufficient"
        if holds_current:
            if not current_target(target):
                return "preference_target_stale"
            return "WITHHOLD"
        if holding and current_target(target) and stored.value == declaration.value:
            return "REAFFIRM"
        if holding and not current_target(target):
            return "UPDATE"
        return "UPDATE" if target.is_archived or stored.value != declaration.value else "DEDUP"
    if holding:
        return "preference_requires_review"
    if target.is_archived:
        return "preference_target_stale"
    return "DEDUP" if stored.value == declaration.value else "preference_conflict"


def preference_provenance(memory: Memory) -> dict[str, Any] | None:
    """Response-only version, including archived management targets."""
    provenance = (memory.metadata_json or {}).get("provenance")
    if preference_declaration(provenance) is not None:
        return {**provenance, "preference_version": target_version(memory)}
    return provenance


async def lock_preference_lifecycle(session, memory: Memory, *, archive: bool):
    """Ledger then memory: the same lock order as preference replacement.

    Keep the archived winner reference as a version-bound tombstone. Archiving
    a superseded revision must not alter the new winner.
    """
    declaration = preference_declaration((memory.metadata_json or {}).get("provenance"))
    if declaration is None:
        return memory
    claim = (await session.execute(select(MemoryClaim).where(
        MemoryClaim.proxy_user_id == memory.proxy_user_id,
        MemoryClaim.claim_fingerprint == preference_identity(declaration).fingerprint,
    ).with_for_update().execution_options(populate_existing=True))).scalar_one_or_none()
    current = (await session.execute(select(Memory).where(
        Memory.id == memory.id, Memory.proxy_user_id == memory.proxy_user_id,
    ).with_for_update().execution_options(populate_existing=True))).scalar_one_or_none()
    if current is None:
        from api.errors import APIError
        raise APIError(status_code=404, code="MEM_404", error="memory_not_found")
    if archive and claim is not None and claim.active_memory_id == current.id:
        claim.status = "archived"
        if claim.winning_revision_id is not None:
            revision = await session.get(MemoryClaimRevision, claim.winning_revision_id)
            if revision is not None:
                revision.status = "archived"
    return current


def legacy_memory_predicate():
    # Declared preferences are available only through explicit context reads.
    # Missing applicability on old memories is not evidence of global scope.
    return Memory.metadata_json["provenance"]["extraction_evidence"][PREFERENCE_MARKER].astext.is_(None)
