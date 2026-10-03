"""Bounded preference policy over the existing memory/claim transaction.

Property and applicability are application assertions, not linguistic proof.
They never inherit registered-writer authority. No classifier or vector search
chooses a replacement; an owned, version-bound exact identity does.
"""
from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from api.db.models import Memory, MemoryCategory, MemoryClaim
from api.schemas.memory_schemas import ExtractedMemory
from api.schemas.preferences import PreferenceApplicability, PreferenceClaimRequest
from api.services.claim_ledger_service import build_domain_claim_identity
from api.services.extraction_service import ExtractionService
from api.services.source_review_service import target_version

PREFERENCE_MARKER = "explicit_preference_v1"


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
    if declaration.applicability.kind == "unresolved" or declaration.commitment == "uncertain":
        return "preference_requires_review"
    if ledger.active_memory_id is None:
        return "preference_target_stale" if declaration.commitment == "replacement" else "NEW"
    if target is None or target.is_archived or str(target.id) != str(ledger.active_memory_id):
        return "preference_target_stale"
    stored = preference_declaration((target.metadata_json or {}).get("provenance"))
    if stored is None or preference_identity(stored).fingerprint != preference_identity(declaration).fingerprint:
        return "preference_identity_mismatch"
    if ledger.status != "active" or ledger.active_value != stored.value:
        return "preference_ledger_inconsistent"
    if declaration.commitment == "replacement":
        if declaration.replaces_memory_id != str(target.id) or declaration.replaces_version != target_version(target):
            return "preference_target_stale"
        # The caller cannot upgrade its application assertion by citing a writer
        # event, or overwrite a future higher-authority preference.
        if int(ledger.authority_priority) > 20:
            return "preference_authority_insufficient"
        return "UPDATE" if stored.value != declaration.value else "DEDUP"
    return "DEDUP" if stored.value == declaration.value else "preference_conflict"


def legacy_memory_predicate():
    # Declared preferences are available only through explicit context reads.
    # Missing applicability on old memories is not evidence of global scope.
    return Memory.metadata_json["provenance"]["extraction_evidence"][PREFERENCE_MARKER].astext.is_(None)
