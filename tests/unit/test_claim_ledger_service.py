from __future__ import annotations

import uuid

import pytest
from datetime import UTC
from datetime import datetime
from types import SimpleNamespace

from api.db.models import Memory
from api.db.models import MemoryCategory
from api.db.models import MemoryClaim
from api.db.models import MemoryClaimRevision
from api.services.claim_ledger_service import ClaimLedgerService
from api.services.claim_ledger_service import build_claim_identity
from api.services.claim_ledger_service import domain_authority_priority
from api.services.claim_versions import CLAIM_PROCESSOR_VERSION
from api.services.claim_versions import CLAIM_SCHEMA_VERSION


class _NoClaimResult:
    def scalar_one_or_none(self):
        return None


class FakeSession:
    def __init__(self) -> None:
        self.added = []
        self.revisions = {}

    def add(self, obj):
        self.added.append(obj)
        if isinstance(obj, MemoryClaimRevision):
            self.revisions[obj.id] = obj

    def execute(self, stmt):
        return _NoClaimResult()

    def flush(self):
        for obj in self.added:
            if getattr(obj, "id", None) is None:
                obj.id = uuid.uuid4()
            if isinstance(obj, MemoryClaimRevision):
                self.revisions[obj.id] = obj

    def get(self, model, obj_id):
        if model is MemoryClaimRevision:
            return self.revisions.get(obj_id)
        return None


def make_memory(
    content: str, *, confidence: float = 1.0, archived: bool = False
) -> Memory:
    return Memory(
        id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        proxy_user_id=uuid.uuid4(),
        content=content,
        category=MemoryCategory.fact,
        importance_score=7.0,
        confidence_score=confidence,
        embedding_id=str(uuid.uuid4()),
        embedding_model_id="text-embedding-004",
        source_conversation_id=uuid.uuid4(),
        source_event_id=uuid.uuid4(),
        is_archived=archived,
    )


def test_plan_claims_share_fingerprint_when_value_changes() -> None:
    starter = build_claim_identity(
        content="Customer's current subscription plan is Starter.",
        category="fact",
        scope={"workspace": "ws_1"},
    )
    growth = build_claim_identity(
        content="Customer's current subscription plan is Growth.",
        category="fact",
        scope={"workspace": "ws_1"},
    )

    assert starter.fingerprint == growth.fingerprint
    assert starter.predicate_key == "current subscription plan"
    assert starter.value == "starter"
    assert growth.value == "growth"


def test_claim_scope_participates_in_identity() -> None:
    first = build_claim_identity(
        content="Customer's current subscription plan is Growth.",
        category="fact",
        scope={"workspace": "ws_1"},
    )
    second = build_claim_identity(
        content="Customer's current subscription plan is Growth.",
        category="fact",
        scope={"workspace": "ws_2"},
    )

    assert first.fingerprint != second.fingerprint


def test_record_memory_creates_active_claim_and_revision() -> None:
    session = FakeSession()
    memory = make_memory("Customer's current subscription plan is Growth.")
    memory.effective_from = datetime(2026, 8, 12, tzinfo=UTC)
    memory.effective_until = datetime(2026, 9, 12, tzinfo=UTC)

    claim = ClaimLedgerService(session).record_memory(
        memory,
        tenant_id=uuid.uuid4(),
        proxy_user_id=memory.proxy_user_id,
        provenance={
            "authority_rules": {"categories": {"fact": 70}},
            "observed_at": "2026-06-20T10:00:00Z",
            "writer_id": str(uuid.uuid4()),
            "evidence": [{"source_type": "subscription", "reference": "sub_1"}],
        },
        resolution="NEW",
    )

    revisions = [
        item for item in session.added if isinstance(item, MemoryClaimRevision)
    ]
    assert claim is not None
    assert claim.status == "active"
    assert claim.active_value == "growth"
    assert claim.active_memory_id == memory.id
    assert claim.authority_priority == 70
    assert len(revisions) == 1
    assert revisions[0].status == "activated"
    assert revisions[0].asserted_value == "growth"
    assert revisions[0].schema_version == CLAIM_SCHEMA_VERSION
    assert revisions[0].processor_version == CLAIM_PROCESSOR_VERSION
    assert revisions[0].effective_from == memory.effective_from
    assert revisions[0].effective_until == memory.effective_until


def test_higher_authority_revision_becomes_winner() -> None:
    session = FakeSession()
    tenant_id = uuid.uuid4()
    proxy_user_id = uuid.uuid4()
    old_revision = MemoryClaimRevision(
        id=uuid.uuid4(),
        claim_id=uuid.uuid4(),
        asserted_value="starter",
        status="activated",
    )
    session.revisions[old_revision.id] = old_revision
    existing_claim = MemoryClaim(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        proxy_user_id=proxy_user_id,
        category=MemoryCategory.fact,
        claim_fingerprint="existing",
        subject_key="user",
        predicate_key="current subscription plan",
        active_value="starter",
        status="active",
        active_memory_id=uuid.uuid4(),
        winning_revision_id=old_revision.id,
        authority_priority=40,
        confidence_score=0.8,
        observed_at=datetime(2026, 6, 20, 9, 0, tzinfo=UTC),
    )
    memory = make_memory(
        "Customer's current subscription plan is Growth.", confidence=0.9
    )
    service = ClaimLedgerService(session)
    service._find_claim = lambda **kwargs: existing_claim  # type: ignore[method-assign]

    service.record_memory(
        memory,
        tenant_id=tenant_id,
        proxy_user_id=proxy_user_id,
        provenance={
            "authority_rules": {"categories": {"fact": 90}},
            "observed_at": "2026-06-20T10:00:00Z",
        },
        resolution="UPDATE",
    )

    new_revision = [
        item for item in session.added if isinstance(item, MemoryClaimRevision)
    ][0]
    assert old_revision.status == "superseded"
    assert existing_claim.status == "active"
    assert existing_claim.active_value == "growth"
    assert existing_claim.active_memory_id == memory.id
    assert new_revision.status == "activated"


def test_equal_authority_different_value_marks_claim_disputed_without_switching_winner() -> (
    None
):
    session = FakeSession()
    tenant_id = uuid.uuid4()
    proxy_user_id = uuid.uuid4()
    existing_claim = MemoryClaim(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        proxy_user_id=proxy_user_id,
        category=MemoryCategory.fact,
        claim_fingerprint="existing",
        subject_key="user",
        predicate_key="current subscription plan",
        active_value="starter",
        status="active",
        active_memory_id=uuid.uuid4(),
        authority_priority=50,
        confidence_score=0.9,
        observed_at=datetime(2026, 6, 20, 10, 0, tzinfo=UTC),
    )
    memory = make_memory(
        "Customer's current subscription plan is Growth.", confidence=0.91
    )
    service = ClaimLedgerService(session)
    service._find_claim = lambda **kwargs: existing_claim  # type: ignore[method-assign]

    service.record_memory(
        memory,
        tenant_id=tenant_id,
        proxy_user_id=proxy_user_id,
        provenance={
            "authority_rules": {"categories": {"fact": 50}},
            "observed_at": "2026-06-20T10:00:00Z",
        },
        resolution="CLARIFICATION_PENDING",
    )

    revision = [
        item for item in session.added if isinstance(item, MemoryClaimRevision)
    ][0]
    assert existing_claim.status == "disputed"
    assert existing_claim.active_value == "starter"
    assert existing_claim.active_memory_id != memory.id
    assert revision.status == "disputed"


class _ScalarRows:
    def __init__(self, rows):
        self.rows = rows

    def scalars(self):
        return self

    def all(self):
        return self.rows


class FakeAsyncLedgerSession:
    def __init__(self, revision_rows, claim_rows):
        self.result_sets = [revision_rows, claim_rows]

    async def execute(self, _statement):
        return _ScalarRows(self.result_sets.pop(0))


@pytest.mark.asyncio
async def test_conflict_selection_updates_claim_winner() -> None:
    tenant_id = uuid.uuid4()
    proxy_user_id = uuid.uuid4()
    memory_a = make_memory("Customer's current subscription plan is Starter.")
    memory_b = make_memory(
        "Customer's current subscription plan is Growth.", archived=True
    )
    claim = MemoryClaim(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        proxy_user_id=proxy_user_id,
        category=MemoryCategory.fact,
        claim_fingerprint="claim-fingerprint",
        subject_key="user",
        predicate_key="current subscription plan",
        active_value="starter",
        status="disputed",
        active_memory_id=memory_a.id,
        authority_priority=50,
        confidence_score=1.0,
    )
    revision_a = MemoryClaimRevision(
        id=uuid.uuid4(),
        claim_id=claim.id,
        memory_id=memory_a.id,
        asserted_value="starter",
        status="activated",
        authority_priority=50,
        confidence_score=1.0,
    )
    revision_b = MemoryClaimRevision(
        id=uuid.uuid4(),
        claim_id=claim.id,
        memory_id=memory_b.id,
        asserted_value="growth",
        status="disputed",
        authority_priority=50,
        confidence_score=1.0,
    )
    revision_a.claim = claim
    revision_b.claim = claim

    await ClaimLedgerService(
        FakeAsyncLedgerSession([revision_a, revision_b], [claim])
    ).apply_conflict_selection(
        memory_a=memory_a,
        memory_b=memory_b,
        selection="B",
        reason="Billing service record confirmed the current plan.",
    )

    assert revision_a.status == "rejected"
    assert revision_b.status == "activated"
    assert claim.status == "active"
    assert claim.active_value == "growth"
    assert claim.active_memory_id == memory_b.id
    assert claim.winning_revision_id == revision_b.id


def test_domain_fields_create_source_backed_revisions() -> None:
    session = FakeSession()
    writer_id = uuid.uuid4()
    source_event_id = uuid.uuid4()
    record = SimpleNamespace(
        id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        proxy_user_id=uuid.uuid4(),
        weak_topics=[{"topic": "integration", "severity": "moderate"}],
        language_profile={"primary": "Hindi"},
    )
    service = ClaimLedgerService(session)
    service._provenance_for_job = lambda _job_id: {  # type: ignore[method-assign]
        "source_event_id": str(source_event_id),
        "writer_id": str(writer_id),
        "observed_at": "2026-06-20T10:00:00Z",
        "scope": {"course": "calculus"},
        "evidence": [{"source_type": "lesson", "reference": "lesson-42"}],
        "authority_rules": {"domain_fields": {"edtech.weak_topics": 85}},
    }

    claims = service.record_domain_fields(
        domain_record=record,
        domain="edtech",
        fields_updated={"weak_topics", "language_profile"},
        field_categories={
            "weak_topics": "expertise",
            "language_profile": "preference",
        },
        job_id=str(uuid.uuid4()),
    )

    revisions = [
        item for item in session.added if isinstance(item, MemoryClaimRevision)
    ]
    assert len(claims) == 2
    assert {claim.predicate_key for claim in claims} == {
        "edtech.weak_topics",
        "edtech.language_profile",
    }
    weak_topic_revision = next(
        revision for revision in revisions if revision.source_field == "weak_topics"
    )
    assert weak_topic_revision.source_domain == "edtech"
    assert weak_topic_revision.source_domain_record_id == str(record.id)
    assert weak_topic_revision.source_event_id == source_event_id
    assert weak_topic_revision.source_writer_id == writer_id
    assert weak_topic_revision.authority_priority == 85
    assert weak_topic_revision.evidence_refs == [
        {"source_type": "lesson", "reference": "lesson-42"}
    ]


def test_domain_field_authority_overrides_category_authority() -> None:
    provenance = {
        "authority_rules": {
            "default_priority": 40,
            "categories": {"fact": 60},
            "domain_fields": {"support.current_open_issue": 95},
        }
    }

    assert (
        domain_authority_priority(
            provenance,
            "support",
            "current_open_issue",
            "fact",
        )
        == 95
    )
    assert (
        domain_authority_priority(
            provenance,
            "support",
            "customer_identity",
            "fact",
        )
        == 60
    )
