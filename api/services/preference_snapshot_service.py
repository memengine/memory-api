"""Read-only, detached preference bindings. Not registered in any live pipeline.

The catalog is a backend-supplied contract, not a model-discovered namespace or
an authorization mechanism. No benchmark imports, provider calls, or writes.
"""
from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import asdict, dataclass

from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import and_, select
from sqlalchemy.orm import Session, sessionmaker

from api.db.models import (
    Memory,
    MemoryCategory,
    MemoryClaim,
    MemoryClaimRevision,
    ProxyUser,
    User,
)
from api.schemas.preferences import PreferenceApplicability, PropertyKey
from api.services.claim_ledger_service import authority_priority
from api.services.preference_claim_service import (
    CHOICE_STATE_MARKER,
    current_target,
    held_choice,
    preference_declaration,
    preference_fingerprint,
    preference_identity,
)
from api.services.source_review_service import target_version

MAX_SNAPSHOT_IDENTITIES = 32


class _Closed(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class SnapshotProperty(_Closed):
    key: PropertyKey
    description: str = Field(min_length=1, max_length=400)


class SnapshotScope(_Closed):
    ref: str = Field(min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9_-]*$")
    applicability: PreferenceApplicability
    description: str = Field(min_length=1, max_length=400)


class PreferenceSnapshotCatalog(_Closed):
    tenant_id: uuid.UUID
    properties: tuple[SnapshotProperty, ...] = Field(min_length=1, max_length=16)
    scopes: tuple[SnapshotScope, ...] = Field(min_length=1, max_length=32)

    @model_validator(mode="after")
    def bounded_identities(self):
        if len(self.properties) * len(self.scopes) > MAX_SNAPSHOT_IDENTITIES:
            raise ValueError("snapshot identity limit exceeded; do not truncate")
        if len({p.key for p in self.properties}) != len(self.properties):
            raise ValueError("duplicate property")
        if len({s.ref for s in self.scopes}) != len(self.scopes):
            raise ValueError("duplicate scope reference")
        fingerprints = []
        for scope in self.scopes:
            if scope.applicability.kind == "unresolved":
                raise ValueError("snapshot requires known applicability")
            fingerprints.extend(preference_fingerprint(p.key, scope.applicability) for p in self.properties)
        if len(set(fingerprints)) != len(fingerprints):
            raise ValueError("duplicate normalized identity")
        return self


@dataclass(frozen=True, slots=True)
class PreferenceTargetSnapshot:
    __pydantic_config__ = ConfigDict(extra="forbid", strict=True)
    memory_id: uuid.UUID
    value: str
    memory_version: str
    authority: int
    is_current: bool
    choice_state: str


@dataclass(frozen=True, slots=True)
class PreferenceIdentitySnapshot:
    __pydantic_config__ = ConfigDict(extra="forbid", strict=True)
    property_key: str
    scope_ref: str
    fingerprint: str
    claim_id: uuid.UUID | None
    target: PreferenceTargetSnapshot | None
    binding_version: str


@dataclass(frozen=True, slots=True)
class PreferenceSnapshot:
    __pydantic_config__ = ConfigDict(extra="forbid", strict=True)
    tenant_id: uuid.UUID
    proxy_user_id: uuid.UUID
    catalog: PreferenceSnapshotCatalog
    identities: tuple[PreferenceIdentitySnapshot, ...]
    binding_version: str


class PreferenceSnapshotUnavailable(ValueError):
    """No partial or falsely empty snapshot may escape a failed read."""


def _digest(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()


def identity_binding_version(claim, target: PreferenceTargetSnapshot) -> str:
    """Shared read/write comparison; neither a signature nor permission."""
    return _digest({
        "claim_id": claim.id, "status": claim.status, "active_memory_id": claim.active_memory_id,
        "winning_revision_id": claim.winning_revision_id, "active_value": claim.active_value,
        "authority": claim.authority_priority, "target": asdict(target), "updated_at": claim.updated_at,
    })


def snapshot_binding_version(snapshot: PreferenceSnapshot) -> str:
    return _digest({
        "tenant_id": snapshot.tenant_id, "proxy_user_id": snapshot.proxy_user_id,
        "catalog": snapshot.catalog.model_dump(mode="json"),
        "identities": [asdict(item) for item in snapshot.identities],
    })


def validate_snapshot_coverage(snapshot: PreferenceSnapshot) -> None:
    """Validate a persisted detached snapshot without pretending it is current."""
    offered = {(p.key, s.ref): preference_fingerprint(p.key, s.applicability)
        for p in snapshot.catalog.properties for s in snapshot.catalog.scopes}
    actual = {(item.property_key, item.scope_ref): item.fingerprint for item in snapshot.identities}
    if (snapshot.catalog.tenant_id != snapshot.tenant_id or offered != actual
        or len(actual) != len(snapshot.identities) or snapshot.binding_version != snapshot_binding_version(snapshot)
        or any((item.claim_id is None) != (item.target is None) for item in snapshot.identities)):
        raise ValueError("invalid persisted snapshot coverage")
    for item in snapshot.identities:
        if item.target is None and item.binding_version != _digest({"absent": item.fingerprint}):
            raise ValueError("invalid persisted absence binding")
        versions = [item.binding_version]
        if item.target is not None:
            versions.append(item.target.memory_version)
            if not (0 <= item.target.authority <= 100 and 1 <= len(item.target.value) <= 256
                and item.target.choice_state in {"current", "current_choice_unresolved"}):
                raise ValueError("invalid persisted target")
        if any(len(value) != 64 or any(char not in "0123456789abcdef" for char in value) for value in versions):
            raise ValueError("invalid persisted binding version")


def snapshot_target(claim, memory, revision, *, property_key, applicability):
    if memory is None or revision is None:
        raise PreferenceSnapshotUnavailable("ledger_target_unavailable")
    try:
        declaration = preference_declaration((memory.metadata_json or {}).get("provenance"))
        holding = held_choice(memory, claim)
        marker = (memory.metadata_json or {}).get(CHOICE_STATE_MARKER) or {}
        if declaration is None:
            raise PreferenceSnapshotUnavailable("ledger_inconsistent")
        identity = preference_identity(declaration)
        valid = (
            declaration.property_key == property_key
            and declaration.applicability == applicability
            and identity.fingerprint == claim.claim_fingerprint
            and claim.category == memory.category == MemoryCategory.preference
            and claim.active_value == declaration.value == revision.asserted_value
            and claim.status in {"active", "archived", "disputed"}
            and (claim.status == "archived") == memory.is_archived
            and (claim.status == "disputed") == holding
            and (marker.get("state") == "current_choice_unresolved") == holding
            and revision.status == ("archived" if memory.is_archived else "activated")
            and 0 <= claim.authority_priority <= 100
            and claim.scope == identity.scope
        )
        if not valid:
            raise PreferenceSnapshotUnavailable("ledger_inconsistent")
        authority = max(claim.authority_priority,
            authority_priority((memory.metadata_json or {}).get("provenance"), "preference"))
    except (TypeError, KeyError, AttributeError, ValueError) as exc:
        raise PreferenceSnapshotUnavailable("ledger_inconsistent") from exc
    return PreferenceTargetSnapshot(
        memory_id=memory.id, value=declaration.value, memory_version=target_version(memory),
        authority=authority, is_current=current_target(memory),
        choice_state="current_choice_unresolved" if holding else "current",
    )


def load_preference_snapshot(
    session_factory: sessionmaker[Session], *, tenant_id: uuid.UUID | str,
    proxy_user_id: uuid.UUID | str, catalog: PreferenceSnapshotCatalog,
) -> PreferenceSnapshot:
    """One bounded SQL SELECT in an owned short-lived session; closes before return.

    No row locks, caller session commit/rollback, autoflush, vector search, or
    absence inferred from a page. A row without a usable winner blocks the whole
    snapshot, including deletion tombstones and incomplete ledger reservations.
    The digest is a comparison token, NOT a lock, signature, or write permission.
    """
    tenant_id, proxy_user_id, catalog = _snapshot_inputs(tenant_id, proxy_user_id, catalog)
    with session_factory() as session:
        return _read_snapshot(session, tenant_id, proxy_user_id, catalog)


def _snapshot_inputs(tenant_id, proxy_user_id, catalog):
    tenant_id, proxy_user_id = uuid.UUID(str(tenant_id)), uuid.UUID(str(proxy_user_id))
    catalog = PreferenceSnapshotCatalog.model_validate(catalog.model_dump(mode="json"))
    if catalog.tenant_id != tenant_id:
        raise PreferenceSnapshotUnavailable("catalog_tenant_mismatch")
    return tenant_id, proxy_user_id, catalog


def read_preference_snapshot(
    session: Session, *, tenant_id: uuid.UUID | str, proxy_user_id: uuid.UUID | str,
    catalog: PreferenceSnapshotCatalog,
) -> PreferenceSnapshot:
    """Same bounded reader within an existing short transaction; no connection nesting.

    Does not flush, commit, close or otherwise own the supplied session. Used by
    inactive admission capture; never hold this transaction over a provider call.
    """
    return _read_snapshot(session, *_snapshot_inputs(tenant_id, proxy_user_id, catalog))


def _read_snapshot(session, tenant_id, proxy_user_id, catalog):
    offered = {
        preference_fingerprint(p.key, s.applicability): (p.key, s)
        for p in catalog.properties for s in catalog.scopes
    }
    # Start from the owner so a legitimate empty profile still yields a row.
    # Filter foreign memories/revisions in JOINs; never return their contents.
    query = select(ProxyUser, MemoryClaim, Memory, MemoryClaimRevision, User.is_active).select_from(ProxyUser).outerjoin(
        User, User.external_id == f"proxy::{proxy_user_id}",
    ).outerjoin(MemoryClaim, and_(
        MemoryClaim.tenant_id == tenant_id, MemoryClaim.proxy_user_id == ProxyUser.id,
        MemoryClaim.claim_fingerprint.in_(offered),
    )).outerjoin(Memory, and_(
        Memory.id == MemoryClaim.active_memory_id, Memory.proxy_user_id == ProxyUser.id,
        Memory.user_id == User.id,
    )).outerjoin(MemoryClaimRevision, and_(
        MemoryClaimRevision.id == MemoryClaim.winning_revision_id,
        MemoryClaimRevision.claim_id == MemoryClaim.id, MemoryClaimRevision.memory_id == Memory.id,
    )).where(ProxyUser.id == proxy_user_id, ProxyUser.tenant_id == tenant_id).limit(len(offered) + 1)
    with session.no_autoflush:
        rows = session.execute(query).all()
        if not rows or any(owner.is_blocked or backing_active is False for owner, _, _, _, backing_active in rows):
            raise PreferenceSnapshotUnavailable("principal_unavailable")
        bindings = {}
        for _, claim, memory, revision, _ in rows:
            if claim is None:
                continue
            if claim.claim_fingerprint not in offered or claim.claim_fingerprint in bindings:
                raise PreferenceSnapshotUnavailable("ledger_inconsistent")
            key, scope = offered[claim.claim_fingerprint]
            target = snapshot_target(claim, memory, revision, property_key=key, applicability=scope.applicability)
            bindings[claim.claim_fingerprint] = (claim.id, target, identity_binding_version(claim, target))
        identities = tuple(PreferenceIdentitySnapshot(
            property_key=key, scope_ref=scope.ref, fingerprint=fingerprint,
            claim_id=bindings[fingerprint][0] if fingerprint in bindings else None,
            target=bindings[fingerprint][1] if fingerprint in bindings else None,
            binding_version=bindings[fingerprint][2] if fingerprint in bindings else _digest({"absent": fingerprint}),
        ) for fingerprint, (key, scope) in sorted(offered.items()))
        provisional = PreferenceSnapshot(tenant_id, proxy_user_id, catalog, identities, "")
        return PreferenceSnapshot(tenant_id, proxy_user_id, catalog, identities, snapshot_binding_version(provisional))
