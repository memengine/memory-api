from __future__ import annotations

import uuid
from datetime import UTC, datetime

from api.db.models import Memory, MemoryCategory, MemoryClaim, MemoryClaimRevision
from api.services.claim_ledger_service import ClaimLedgerService


class Session:
    def __init__(self, revision: MemoryClaimRevision | None = None) -> None:
        self.added = []
        self.revisions = {revision.id: revision} if revision is not None else {}

    def execute(self, _statement):
        return None

    def add(self, item) -> None:
        self.added.append(item)
        if isinstance(item, MemoryClaimRevision):
            self.revisions[item.id] = item

    def flush(self) -> None:
        for item in self.added:
            if getattr(item, "id", None) is None:
                item.id = uuid.uuid4()

    def get(self, model, identifier):
        if model is MemoryClaimRevision:
            return self.revisions.get(identifier)
        return None


def memory(content: str) -> Memory:
    return Memory(
        id=uuid.uuid4(), user_id=uuid.uuid4(), proxy_user_id=uuid.uuid4(),
        content=content, category=MemoryCategory.fact, importance_score=5.0,
        confidence_score=0.95, embedding_id=str(uuid.uuid4()),
        embedding_model_id="text-embedding-004",
        source_conversation_id=uuid.uuid4(), source_event_id=uuid.uuid4(),
        metadata_json={}, is_archived=False,
    )


def test_update_reconciles_using_predecessor_revision_despite_new_fingerprint() -> None:
    tenant_id = uuid.uuid4()
    proxy_user_id = uuid.uuid4()
    old_memory = memory("User lives in Chennai.")
    old_memory.proxy_user_id = proxy_user_id
    claim = MemoryClaim(
        id=uuid.uuid4(), tenant_id=tenant_id, proxy_user_id=proxy_user_id,
        category=MemoryCategory.fact, claim_fingerprint="old-fingerprint",
        subject_key="user", predicate_key="user lives in chennai", scope={},
        active_value="user lives in chennai", status="active",
        active_memory_id=old_memory.id, authority_priority=50,
        confidence_score=0.95, observed_at=datetime(2026, 7, 1, tzinfo=UTC),
    )
    old_revision = MemoryClaimRevision(
        id=uuid.uuid4(), claim_id=claim.id, memory_id=old_memory.id,
        asserted_value="user lives in chennai", status="activated",
        authority_priority=50, confidence_score=0.95,
    )
    claim.winning_revision_id = old_revision.id
    session = Session(old_revision)
    service = ClaimLedgerService(session)
    service._find_claim_for_memory = lambda _memory_id: claim  # type: ignore[method-assign]
    new_memory = memory("Correction: User now lives in Bengaluru.")
    new_memory.proxy_user_id = proxy_user_id
    evidence = {"action": "UPDATE", "reason_codes": ["incoming_source_wins"]}

    result = service.record_memory(
        new_memory, tenant_id=tenant_id, proxy_user_id=proxy_user_id,
        provenance={
            "observed_at": "2026-07-02T00:00:00Z",
            "evidence": [{"source_type": "turn", "reference": "turn-2"}],
        },
        resolution="UPDATE", decision_evidence=evidence,
        predecessor_memory_id=old_memory.id,
    )

    new_revision = next(
        item for item in session.added if isinstance(item, MemoryClaimRevision)
    )
    assert result is claim
    assert old_revision.status == "superseded"
    assert new_revision.claim_id == claim.id
    assert new_revision.status == "activated"
    assert new_revision.decision_evidence == evidence
    assert claim.active_memory_id == new_memory.id
    assert claim.winning_revision_id == new_revision.id


def test_keep_both_does_not_use_predecessor_reconciliation() -> None:
    session = Session()
    service = ClaimLedgerService(session)
    service._find_claim_for_memory = lambda _memory_id: (_ for _ in ()).throw(  # type: ignore[method-assign]
        AssertionError("KEEP_BOTH must not reconcile a predecessor claim")
    )
    service._find_claim = lambda **_kwargs: None  # type: ignore[method-assign]
    new_memory = memory("User speaks English.")

    claim = service.record_memory(
        new_memory, tenant_id=uuid.uuid4(), proxy_user_id=new_memory.proxy_user_id,
        provenance=None, resolution="KEEP_BOTH",
        predecessor_memory_id=uuid.uuid4(),
    )

    assert claim is not None
    assert claim.active_memory_id == new_memory.id
