from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select

from api.db.models import PermissionGrant
from api.db.models import UniversalMemory
from api.db.models import UniversalMemoryClaim
from api.db.models import UniversalMemoryClaimRevision
from api.services.claim_ledger_service import build_claim_identity
from api.services.claim_ledger_service import normalize_text


@dataclass(slots=True)
class UniversalClaimDecision:
    claim_id: uuid.UUID
    status: str
    memory_is_active: bool


@dataclass(slots=True)
class UniversalClaimProvenance:
    claim_status: str
    revision_status: str
    source_type: str
    grant_status: str
    recorded_at: datetime
    resolution_reason: str | None


class UniversalClaimLedgerService:
    """Write-side governance for Passport memories.

    Tenant claims and Passport claims deliberately use separate tables. This
    prevents a universal user from being coupled to one tenant's proxy-user
    namespace and keeps normal vector retrieval free of claim-table joins.
    """

    @staticmethod
    def record_sync(
        session: Any,
        memory: UniversalMemory,
        *,
        grant: PermissionGrant | None,
        source_tenant_id: uuid.UUID | None,
        resolution_reason: str,
    ) -> UniversalClaimDecision:
        identity = UniversalClaimLedgerService._identity(memory)
        claim = session.execute(
            select(UniversalMemoryClaim).where(
                UniversalMemoryClaim.user_uui_id == memory.user_uui_id,
                UniversalMemoryClaim.claim_fingerprint == identity.fingerprint,
            )
        ).scalar_one_or_none()
        if claim is not None and not isinstance(claim, UniversalMemoryClaim):
            claim = None
        return UniversalClaimLedgerService._record(
            session,
            memory,
            claim=claim,
            identity=identity,
            grant=grant,
            source_tenant_id=source_tenant_id,
            resolution_reason=resolution_reason,
        )

    @staticmethod
    async def record_async(
        session: Any,
        memory: UniversalMemory,
        *,
        grant: PermissionGrant | None,
        source_tenant_id: uuid.UUID | None,
        resolution_reason: str,
    ) -> UniversalClaimDecision:
        identity = UniversalClaimLedgerService._identity(memory)
        claim = (
            await session.execute(
                select(UniversalMemoryClaim).where(
                    UniversalMemoryClaim.user_uui_id == memory.user_uui_id,
                    UniversalMemoryClaim.claim_fingerprint == identity.fingerprint,
                )
            )
        ).scalar_one_or_none()
        if claim is not None and not isinstance(claim, UniversalMemoryClaim):
            claim = None
        return UniversalClaimLedgerService._record(
            session,
            memory,
            claim=claim,
            identity=identity,
            grant=grant,
            source_tenant_id=source_tenant_id,
            resolution_reason=resolution_reason,
        )

    @staticmethod
    async def archive_memory_async(
        session: Any,
        *,
        memory: UniversalMemory,
        reason: str,
    ) -> None:
        row = (
            await session.execute(
                select(UniversalMemoryClaimRevision, UniversalMemoryClaim)
                .join(
                    UniversalMemoryClaim,
                    UniversalMemoryClaim.id == UniversalMemoryClaimRevision.claim_id,
                )
                .where(
                    UniversalMemoryClaimRevision.universal_memory_id == memory.id,
                    UniversalMemoryClaim.user_uui_id == memory.user_uui_id,
                )
                .order_by(UniversalMemoryClaimRevision.created_at.desc())
                .limit(1)
            )
        ).first()
        if row is None:
            return
        revision, claim = row
        revision.status = "archived"
        revision.resolution_reason = reason
        if claim.active_memory_id == memory.id:
            claim.status = "archived"
            claim.active_memory_id = None
            claim.active_value = None
            claim.winning_revision_id = None
            claim.updated_at = datetime.now(UTC)

    @staticmethod
    async def provenance_for_memories(
        session: Any,
        *,
        user_uui_id: uuid.UUID,
        memory_ids: list[uuid.UUID],
    ) -> dict[uuid.UUID, UniversalClaimProvenance]:
        if not memory_ids:
            return {}
        rows = (
            await session.execute(
                select(
                    UniversalMemoryClaimRevision,
                    UniversalMemoryClaim,
                    PermissionGrant,
                )
                .join(
                    UniversalMemoryClaim,
                    UniversalMemoryClaim.id == UniversalMemoryClaimRevision.claim_id,
                )
                .outerjoin(
                    PermissionGrant,
                    PermissionGrant.id == UniversalMemoryClaimRevision.permission_grant_id,
                )
                .where(
                    UniversalMemoryClaim.user_uui_id == user_uui_id,
                    UniversalMemoryClaimRevision.universal_memory_id.in_(memory_ids),
                )
                .order_by(UniversalMemoryClaimRevision.created_at.desc())
            )
        ).all()
        now = datetime.now(UTC)
        result: dict[uuid.UUID, UniversalClaimProvenance] = {}
        for revision, claim, grant in rows:
            memory_id = revision.universal_memory_id
            if memory_id is None or memory_id in result:
                continue
            if grant is None or revision.source_type in {"user_correction", "system"}:
                grant_status = "not_required"
            elif not bool(grant.is_active) or grant.revoked_at is not None:
                grant_status = "revoked"
            elif grant.expires_at is not None and grant.expires_at <= now:
                grant_status = "expired"
            else:
                grant_status = "active"
            result[memory_id] = UniversalClaimProvenance(
                claim_status=str(claim.status),
                revision_status=str(revision.status),
                source_type=str(revision.source_type),
                grant_status=grant_status,
                recorded_at=revision.created_at,
                resolution_reason=revision.resolution_reason,
            )
        return result

    @staticmethod
    def _identity(memory: UniversalMemory):
        category = memory.category.value if hasattr(memory.category, "value") else str(memory.category)
        return build_claim_identity(content=memory.content, category=category, scope={})



    @staticmethod
    def backfill_revision_sync(
        session: Any,
        memory: UniversalMemory,
        *,
        claim: UniversalMemoryClaim | None,
        current_revision: UniversalMemoryClaimRevision | None,
        claim_is_backfill_managed: bool,
        grant: PermissionGrant | None,
        source_tenant_id: uuid.UUID | None,
        resolution_reason: str,
    ) -> tuple[UniversalClaimDecision, UniversalMemoryClaim, UniversalMemoryClaimRevision]:
        """Create missing legacy provenance without mutating the memory row.

        Existing live-ledger winners and user corrections are immutable from the
        backfill. Claims created exclusively by this backfill may advance to a
        newer active legacy record so UUID scan order cannot choose the winner.
        """
        identity = UniversalClaimLedgerService._identity(memory)
        recorded_at = memory.created_at or datetime.now(UTC)
        memory_is_active = not bool(memory.is_archived)
        source_type = str(memory.source_type or "passport_agent")
        revision_status = "activated" if memory_is_active else "archived"

        if claim is None:
            claim = UniversalMemoryClaim(
                id=uuid.uuid4(),
                user_uui_id=memory.user_uui_id,
                category=memory.category.value if hasattr(memory.category, "value") else str(memory.category),
                claim_fingerprint=identity.fingerprint,
                subject_key=identity.subject_key,
                predicate_key=identity.predicate_key,
                active_value=identity.value if memory_is_active else None,
                status="active" if memory_is_active else "archived",
                active_memory_id=memory.id if memory_is_active else None,
                confidence_score=float(memory.confidence or 0.0),
                created_at=recorded_at,
                updated_at=recorded_at,
            )
            session.add(claim)
        else:
            same_value = normalize_text(claim.active_value or "") == normalize_text(identity.value)
            current_recorded_at = current_revision.created_at if current_revision is not None else claim.updated_at
            can_advance_legacy_winner = claim_is_backfill_managed and memory_is_active and (
                source_type == "user_correction"
                or (
                    (current_recorded_at is None or recorded_at > current_recorded_at)
                    and (current_revision is None or current_revision.source_type != "user_correction")
                )
            )

            if can_advance_legacy_winner:
                if current_revision is not None and current_revision.status == "activated":
                    current_revision.status = "superseded"
                claim.active_value = identity.value
                claim.active_memory_id = memory.id
                claim.status = "active" if source_type == "user_correction" else ("active" if same_value else "disputed")
                claim.confidence_score = float(memory.confidence or 0.0)
                claim.updated_at = recorded_at
                revision_status = "activated"
            elif not memory_is_active:
                revision_status = "archived"
            elif same_value:
                revision_status = "asserted"
            else:
                revision_status = "disputed"
                if claim_is_backfill_managed and source_type != "user_correction":
                    claim.status = "disputed"

        revision = UniversalMemoryClaimRevision(
            id=uuid.uuid4(),
            claim_id=claim.id,
            universal_memory_id=memory.id,
            source_tenant_id=source_tenant_id,
            source_agent_id=memory.source_agent_id,
            source_org_connection_id=memory.source_org_connection_id,
            permission_grant_id=getattr(grant, "id", None) if grant is not None else None,
            source_type=source_type,
            asserted_value=identity.value,
            status=revision_status,
            confidence_score=float(memory.confidence or 0.0),
            resolution_reason=resolution_reason,
            created_at=recorded_at,
        )
        session.add(revision)
        if revision_status == "activated":
            claim.winning_revision_id = revision.id
        return (
            UniversalClaimDecision(
                claim_id=claim.id,
                status=str(claim.status),
                memory_is_active=memory_is_active,
            ),
            claim,
            revision,
        )
    @staticmethod
    def _record(
        session: Any,
        memory: UniversalMemory,
        *,
        claim: UniversalMemoryClaim | None,
        identity: Any,
        grant: PermissionGrant | None,
        source_tenant_id: uuid.UUID | None,
        resolution_reason: str,
    ) -> UniversalClaimDecision:
        now = datetime.now(UTC)
        source_type = str(memory.source_type or "passport_agent")
        revision_status = "activated"
        memory_is_active = not bool(memory.is_archived)

        if claim is None:
            claim = UniversalMemoryClaim(
                id=uuid.uuid4(),
                user_uui_id=memory.user_uui_id,
                category=memory.category.value if hasattr(memory.category, "value") else str(memory.category),
                claim_fingerprint=identity.fingerprint,
                subject_key=identity.subject_key,
                predicate_key=identity.predicate_key,
                active_value=identity.value if memory_is_active else None,
                status="active" if memory_is_active else "archived",
                active_memory_id=memory.id if memory_is_active else None,
                confidence_score=float(memory.confidence or 0.0),
                created_at=now,
                updated_at=now,
            )
            session.add(claim)
        else:
            same_value = normalize_text(claim.active_value or "") == normalize_text(identity.value)
            if claim.active_memory_id == memory.id and memory_is_active:
                claim.active_value = identity.value
                claim.status = "active"
                claim.confidence_score = float(memory.confidence or 0.0)
                claim.updated_at = now
            elif source_type == "user_correction" and memory_is_active:
                claim.active_value = identity.value
                claim.active_memory_id = memory.id
                claim.status = "active"
                claim.confidence_score = float(memory.confidence or 1.0)
                claim.updated_at = now
            elif not memory_is_active:
                revision_status = "archived"
            elif same_value:
                memory.is_archived = True
                memory_is_active = False
                revision_status = "asserted"
            else:
                memory.is_archived = True
                memory_is_active = False
                claim.status = "disputed"
                claim.updated_at = now
                revision_status = "disputed"

        revision = UniversalMemoryClaimRevision(
            id=uuid.uuid4(),
            claim_id=claim.id,
            universal_memory_id=memory.id,
            source_tenant_id=source_tenant_id,
            source_agent_id=memory.source_agent_id,
            source_org_connection_id=memory.source_org_connection_id,
            permission_grant_id=getattr(grant, "id", None) if grant is not None else None,
            source_type=source_type,
            asserted_value=identity.value,
            status=revision_status,
            confidence_score=float(memory.confidence or 0.0),
            resolution_reason=resolution_reason,
            created_at=now,
        )
        session.add(revision)
        if revision_status == "activated":
            claim.winning_revision_id = revision.id
        return UniversalClaimDecision(
            claim_id=claim.id,
            status=str(claim.status),
            memory_is_active=memory_is_active,
        )
