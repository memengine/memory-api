"""Minimal server-owned projection of a fixed runtime decision's write targets.

This is not semantic proof, authority, an applied receipt, or permission to return
context. Retrieval remains user-wide until overlap/fallback handling is added.
"""
from __future__ import annotations

import uuid
from typing import Literal

from pydantic import Field, model_validator

from api.services.preference_choice_service import Version
from api.services.preference_fact_transaction import FactTarget
from api.services.preference_intent_admission_service import (
    PreferenceAdmissionUnavailable,
    _Closed,
    _hash,
    _json,
)

MAX_FOOTPRINT_BYTES = 8192


class RuntimePreferenceFootprint(_Closed):
    schema_version: Literal[1] = 1
    job_id: uuid.UUID
    tenant_id: uuid.UUID
    proxy_user_id: uuid.UUID
    capture_id: uuid.UUID
    transcript_sha256: Version
    source_sha256: Version | None
    decision_sha256: Version
    coverage: Literal["targets", "user_learned_context"]
    reason: Literal["known_targets", "empty_decision", "unknown_preference",
                    "unknown_fact_target", "incomplete_fact_context"]
    preference_fingerprints: tuple[Version, ...] = Field(max_length=16)
    fact_targets: tuple[FactTarget, ...] = Field(max_length=16)
    projection_sha256: Version

    @model_validator(mode="after")
    def consistent_projection(self):
        prefs, facts = self.preference_fingerprints, self.fact_targets
        if (len(set(prefs)) != len(prefs)
            or len({t.memory_id for t in facts}) != len(facts)
            or len(prefs) + len(facts) > 16):
            raise ValueError("invalid footprint targets")
        if self.coverage == "targets":
            if self.reason != "known_targets" or not (prefs or facts):
                raise ValueError("target coverage requires nonempty bound targets")
        elif self.reason == "known_targets" or prefs or facts:
            raise ValueError("unknown coverage must retain a user-wide hold")
        raw = self.model_dump(mode="json")
        if (len(_json(raw).encode("utf-8")) > MAX_FOOTPRINT_BYTES
            or self.projection_sha256 != _hash({k: v for k, v in raw.items() if k != "projection_sha256"})):
            raise ValueError("invalid footprint digest or size")
        return self


def decision_footprint(job, record) -> RuntimePreferenceFootprint:
    """Called only after the transaction service validates source and fixed record.

    Target identities come from the backend snapshot, not model-provided IDs.
    Unbound/ambiguous findings and incomplete fact coverage stay user-wide.
    """
    if record.state not in {"validated", "applied"} or record.decision_sha256 is None:
        raise PreferenceAdmissionUnavailable("intent_footprint_decision_missing")
    if job.tenant_id != record.tenant_id or job.proxy_user_id != record.proxy_user_id:
        raise PreferenceAdmissionUnavailable("intent_footprint_owner_changed")
    identities = {(i.property_key, i.scope_ref): i.fingerprint for i in record.snapshot.identities}
    targets = {t.memory_id: t for t in record.fact_context}
    prefs, facts = set(), {}
    reason = "known_targets"
    for finding in record.reply.preferences:
        identity = (finding.property_key, finding.scope_ref)
        if finding.action == "abstain" or identity not in identities:
            reason = "unknown_preference"
            break
        prefs.add(identities[identity])
    if reason == "known_targets" and record.reply.nonpreferences:
        if not record.fact_context_complete:
            reason = "incomplete_fact_context"
        else:
            for plan in record.fact_plans:
                if plan.target_id not in targets:
                    reason = "unknown_fact_target"
                    break
                facts[plan.target_id] = targets[plan.target_id]
    if reason == "known_targets" and not (prefs or facts):
        reason = "empty_decision"
    narrow = reason == "known_targets"
    raw = {"schema_version": 1, "job_id": str(job.id), "tenant_id": str(record.tenant_id),
        "proxy_user_id": str(record.proxy_user_id), "capture_id": str(record.capture_id),
        "transcript_sha256": record.transcript_sha256,
        "source_sha256": record.runtime_source_sha256, "decision_sha256": record.decision_sha256,
        "coverage": "targets" if narrow else "user_learned_context", "reason": reason,
        "preference_fingerprints": sorted(prefs) if narrow else [],
        "fact_targets": [facts[key].model_dump(mode="json") for key in sorted(facts)] if narrow else []}
    return RuntimePreferenceFootprint.model_validate({**raw, "projection_sha256": _hash(raw)})


def check_footprint(job, record) -> RuntimePreferenceFootprint | None:
    """Missing legacy projections are not a target bound; mismatches fail closed."""
    raw = job.runtime_withholding_footprint
    if raw is None:
        return None
    try:
        existing = RuntimePreferenceFootprint.model_validate(raw)
        if existing != decision_footprint(job, record):
            raise ValueError("fixed footprint changed")
        return existing
    except (TypeError, ValueError) as exc:
        raise PreferenceAdmissionUnavailable("intent_footprint_changed") from exc
