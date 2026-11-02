"""Backend source scope survives extraction context, pending storage and ledger."""
import uuid
from types import SimpleNamespace as Row

import pytest

from api.schemas.extraction_schemas import PendingExtractedMemory
from api.tasks import extraction_tasks as tasks
from api.services.claim_ledger_service import build_claim_identity


def candidate():
    return PendingExtractedMemory(content="Project language could become Go.", category="expertise",
        importance_score=7, confidence=0.9, reasoning="Unresolved correction.",
        candidate_reason="source_decision_pending", validated_evidence={"claim_state": "correction"})


@pytest.mark.parametrize("left,right", [("alpha", "beta"), (1, "1"), (True, "True"), (None, "None")])
def test_generic_scope_types_and_values_are_distinct(left, right):
    a = build_claim_identity(content="Project language is Go.", category="expertise", scope={"project": left})
    b = build_claim_identity(content="Project language is Go.", category="expertise", scope={"project": right})
    assert a.fingerprint != b.fingerprint


def test_pending_events_preserve_separate_scope_and_job_associations(monkeypatch):
    tenant, owner, writer = [str(uuid.uuid4()) for _ in range(3)]
    rows = []
    session = Row(add=lambda row: rows.append(row) if row not in rows else None)
    def find(_session, **kw):
        for row in rows:
            if row.candidate_fingerprint == kw["fingerprint"] or tasks._can_reinforce_candidate(row, kw["candidate"]):
                return row, False
        return None, False
    monkeypatch.setattr(tasks, "_find_matching_pending_candidate", find)
    associations = []
    for scope in ("alpha", "beta", 1, "1"):
        job, event = str(uuid.uuid4()), str(uuid.uuid4())
        associations.append((job, event))
        count = tasks._persist_pending_extraction_candidates(session, candidates=[candidate()],
            tenant_id=tenant, proxy_user_id=owner, extraction_job_id=job, source_event_id=event,
            source_provenance={"writer_id": writer, "scope": {"project": scope}})
        assert count == 1
    assert len(rows) == 4
    assert [(str(r.extraction_job_id), str(r.source_event_id)) for r in rows] == associations
    assert all(r.reinforcement_count == 1 for r in rows)
    # Same event/job retry finds its own pending entry, not another project's.
    tasks._persist_pending_extraction_candidates(session, candidates=[candidate()], tenant_id=tenant,
        proxy_user_id=owner, extraction_job_id=associations[0][0], source_event_id=associations[0][1],
        source_provenance={"writer_id": writer, "scope": {"project": "alpha"}})
    assert len(rows) == 4 and rows[0].reinforcement_count == 2


def test_extraction_context_excludes_other_scopes_without_mutating_rows():
    rows = [Row(metadata_json={"provenance": {"scope": {"project": p}}}) for p in (1, "1", "alpha")]
    selected = tasks._scope_extraction_context(rows, {"scope": {"project": "1"}})
    assert selected == [rows[1]]
    assert tasks._scope_extraction_context(rows, None) == rows
    assert len(rows) == 3


def test_new_events_do_not_replace_prior_same_scope_pending_proposal(monkeypatch):
    rows = []
    session = Row(add=lambda row: rows.append(row) if row not in rows else None)
    def find(_session, **kw):
        return next(((row, False) for row in rows if tasks._can_reinforce_candidate(row, kw["candidate"])), (None, False))
    monkeypatch.setattr(tasks, "_find_matching_pending_candidate", find)
    tenant, owner, writer = [str(uuid.uuid4()) for _ in range(3)]
    for _ in range(2):
        tasks._persist_pending_extraction_candidates(session, candidates=[candidate()], tenant_id=tenant,
            proxy_user_id=owner, extraction_job_id=str(uuid.uuid4()), source_event_id=str(uuid.uuid4()),
            source_provenance={"writer_id": writer, "scope": {"project": "alpha"}})
    assert len(rows) == 2


def test_legacy_unbound_pending_cannot_absorb_bound_source(monkeypatch):
    observed = []
    def find(_session, **kw):
        incoming = kw["candidate"]
        legacy = Row(content=incoming.content, metadata_json={"extraction_evidence": {}})
        assert not tasks._can_reinforce_candidate(legacy, incoming)
        observed.append(incoming)
        return None, False
    monkeypatch.setattr(tasks, "_find_matching_pending_candidate", find)
    original = candidate()
    original.validated_evidence["pending_source_binding_v1"] = {"scope": "forged"}
    tasks._persist_pending_extraction_candidates(Row(add=lambda _: None), candidates=[original],
        tenant_id=str(uuid.uuid4()), proxy_user_id=str(uuid.uuid4()), extraction_job_id=str(uuid.uuid4()),
        source_event_id=str(uuid.uuid4()), source_provenance={"writer_id": str(uuid.uuid4()), "scope": {"project": 1}})
    assert observed[0].validated_evidence["pending_source_binding_v1"]["scope"] == {"project": 1}
    assert original.validated_evidence["pending_source_binding_v1"] == {"scope": "forged"}


def test_generic_string_scopes_keep_existing_fingerprints():
    import hashlib
    from api.services.claim_ledger_service import normalize_scope
    scope = {"project": "alpha"}
    identity = build_claim_identity(content="Exact proposition.", category="fact", scope=scope)
    previous = {"category": "fact", "subject": "exact_proposition_v1", "predicate": "Exact proposition.",
                "scope": normalize_scope(scope)}
    assert identity.fingerprint == hashlib.sha256(repr(sorted(previous.items())).encode()).hexdigest()


@pytest.mark.asyncio
async def test_other_scope_memory_does_not_reach_actual_extraction_prompt(tmp_path):
    import json
    from tests.unit.test_conflict_resolver import make_existing_memory
    from tests.unit.test_extraction_service import FakeLLMService, _spec
    from api.services.extraction_service import ExtractionService
    memory = make_existing_memory()
    memory.content = "SENTINEL_ONLY_NUMERIC_SCOPE_CONTEXT"
    memory.metadata_json = {"provenance": {"scope": {"project": 1}}}
    source = {"service": "billing", "scope": {"project": "1"}}
    llm = FakeLLMService(json.dumps({"memories": [], "nothing_to_extract": True}))
    service = ExtractionService(llm_service=llm, spec_path=_spec(tmp_path))
    await service.extract(messages=[{"role": "user", "content": "This project uses Python."}],
        source_context=source, existing_memories=tasks._scope_extraction_context([memory], source))
    assert len(llm.calls) == 1
    assert memory.content not in llm.calls[0]["user_message"]


def test_legacy_numeric_claim_cannot_be_inherited_by_new_string_scope():
    from tests.unit.test_claim_ledger_service import FakeSession, make_memory
    from api.services.claim_ledger_service import ClaimLedgerService
    memory = make_memory("Project language is Go.")
    service = ClaimLedgerService(FakeSession())
    service._find_claim = lambda **_: Row(scope={"project": 1})
    with pytest.raises(ValueError, match="scope binding mismatch"):
        service.record_memory(memory, tenant_id=uuid.uuid4(), proxy_user_id=memory.proxy_user_id,
            provenance={"scope": {"project": "1"}}, resolution="NEW")
