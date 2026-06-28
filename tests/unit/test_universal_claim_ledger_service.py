from __future__ import annotations

import uuid

from api.db.models import UniversalMemory
from api.db.models import UniversalMemoryClaim
from api.db.models import UniversalMemoryClaimRevision
from api.services.claim_ledger_service import build_claim_identity
from api.services.claim_versions import CLAIM_PROCESSOR_VERSION
from api.services.claim_versions import CLAIM_SCHEMA_VERSION
from api.services.claim_versions import PASSPORT_BACKFILL_PROCESSOR_VERSION
from api.services.universal_claim_ledger_service import UniversalClaimLedgerService


class FakeSession:
    def __init__(self) -> None:
        self.added: list[object] = []

    def add(self, value: object) -> None:
        self.added.append(value)

    def get(self, _model, _identity):
        return None


def memory(content: str, *, source_type: str = "passport_agent") -> UniversalMemory:
    return UniversalMemory(
        id=uuid.uuid4(),
        user_uui_id=uuid.uuid4(),
        source_agent_id=uuid.uuid4() if source_type == "passport_agent" else None,
        source_type=source_type,
        content=content,
        category="fact",
        importance_score=7.0,
        confidence=0.9,
        embedding_id=None,
        is_archived=False,
        is_flagged=False,
        metadata_json={},
    )


def identity_for(item: UniversalMemory):
    return build_claim_identity(content=item.content, category="fact", scope={})


def test_first_passport_assertion_becomes_active_claim() -> None:
    session = FakeSession()
    item = memory("User's current plan is Starter")

    decision = UniversalClaimLedgerService._record(
        session,
        item,
        claim=None,
        identity=identity_for(item),
        grant=None,
        source_tenant_id=None,
        resolution_reason="test",
    )

    claim = next(value for value in session.added if isinstance(value, UniversalMemoryClaim))
    revision = next(value for value in session.added if isinstance(value, UniversalMemoryClaimRevision))
    assert decision.memory_is_active is True
    assert claim.active_memory_id == item.id
    assert claim.status == "active"
    assert revision.status == "activated"
    assert revision.schema_version == CLAIM_SCHEMA_VERSION
    assert revision.processor_version == CLAIM_PROCESSOR_VERSION


def test_conflicting_agent_assertion_is_audited_but_not_retrievable() -> None:
    session = FakeSession()
    first = memory("User's current plan is Starter")
    UniversalClaimLedgerService._record(
        session,
        first,
        claim=None,
        identity=identity_for(first),
        grant=None,
        source_tenant_id=None,
        resolution_reason="first",
    )
    claim = next(value for value in session.added if isinstance(value, UniversalMemoryClaim))
    second = memory("User's current plan is Growth")
    second.user_uui_id = first.user_uui_id

    decision = UniversalClaimLedgerService._record(
        session,
        second,
        claim=claim,
        identity=identity_for(second),
        grant=None,
        source_tenant_id=None,
        resolution_reason="conflict",
    )

    assert decision.memory_is_active is False
    assert second.is_archived is True
    assert claim.status == "disputed"
    assert claim.active_memory_id == first.id
    assert isinstance(session.added[-1], UniversalMemoryClaimRevision)
    assert session.added[-1].status == "disputed"


def test_user_correction_becomes_the_claim_winner() -> None:
    session = FakeSession()
    first = memory("User's current plan is Starter")
    UniversalClaimLedgerService._record(
        session,
        first,
        claim=None,
        identity=identity_for(first),
        grant=None,
        source_tenant_id=None,
        resolution_reason="first",
    )
    claim = next(value for value in session.added if isinstance(value, UniversalMemoryClaim))
    correction = memory("User's current plan is Growth", source_type="user_correction")
    correction.user_uui_id = first.user_uui_id

    decision = UniversalClaimLedgerService._record(
        session,
        correction,
        claim=claim,
        identity=identity_for(correction),
        grant=None,
        source_tenant_id=None,
        resolution_reason="corrected by user",
    )

    assert decision.memory_is_active is True
    assert claim.status == "active"
    assert claim.active_memory_id == correction.id
    assert claim.active_value == "growth"


def test_backfill_advances_only_backfill_managed_claim_to_newer_legacy_memory() -> None:
    from datetime import UTC, datetime, timedelta

    session = FakeSession()
    older = memory("User's current plan is Starter")
    older.created_at = datetime.now(UTC) - timedelta(days=2)
    _, claim, older_revision = UniversalClaimLedgerService.backfill_revision_sync(
        session, older, claim=None, current_revision=None,
        claim_is_backfill_managed=True, grant=None, source_tenant_id=None,
        resolution_reason="legacy passport provenance backfill",
    )
    newer = memory("User's current plan is Growth")
    newer.user_uui_id = older.user_uui_id
    newer.created_at = datetime.now(UTC) - timedelta(days=1)

    _, claim, newer_revision = UniversalClaimLedgerService.backfill_revision_sync(
        session, newer, claim=claim, current_revision=older_revision,
        claim_is_backfill_managed=True, grant=None, source_tenant_id=None,
        resolution_reason="legacy passport provenance backfill",
    )

    assert claim.active_memory_id == newer.id
    assert claim.active_value == "growth"
    assert older_revision.status == "superseded"
    assert newer_revision.status == "activated"
    assert newer_revision.schema_version == CLAIM_SCHEMA_VERSION
    assert newer_revision.processor_version == PASSPORT_BACKFILL_PROCESSOR_VERSION
    assert newer_revision.schema_version == CLAIM_SCHEMA_VERSION
    assert newer_revision.processor_version == PASSPORT_BACKFILL_PROCESSOR_VERSION
    assert older.is_archived is False
    assert newer.is_archived is False


def test_backfill_never_replaces_live_ledger_winner() -> None:
    from datetime import UTC, datetime, timedelta

    session = FakeSession()
    live = memory("User's current plan is Starter")
    live.created_at = datetime.now(UTC) - timedelta(days=2)
    UniversalClaimLedgerService._record(
        session, live, claim=None, identity=identity_for(live), grant=None,
        source_tenant_id=None, resolution_reason="live extraction",
    )
    claim = next(value for value in session.added if isinstance(value, UniversalMemoryClaim))
    live_revision = next(value for value in session.added if isinstance(value, UniversalMemoryClaimRevision))
    legacy = memory("User's current plan is Growth")
    legacy.user_uui_id = live.user_uui_id
    legacy.created_at = datetime.now(UTC) - timedelta(days=1)

    _, claim, legacy_revision = UniversalClaimLedgerService.backfill_revision_sync(
        session, legacy, claim=claim, current_revision=live_revision,
        claim_is_backfill_managed=False, grant=None, source_tenant_id=None,
        resolution_reason="legacy passport provenance backfill",
    )

    assert claim.active_memory_id == live.id
    assert claim.active_value == "starter"
    assert live_revision.status == "activated"
    assert legacy_revision.status == "disputed"
    assert legacy.is_archived is False
