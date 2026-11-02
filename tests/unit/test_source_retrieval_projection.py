from types import SimpleNamespace as Row
import uuid
import pytest
from api.services.retriever import RetrieverService, source_winner_matches
from api.services.claim_ledger_service import GENERIC_CLAIM_SUBJECT


def coherent():
    claim_id, memory_id, revision_id, tenant, owner, event = [uuid.uuid4() for _ in range(6)]
    claim = Row(id=claim_id, status="active", active_memory_id=memory_id,
        winning_revision_id=revision_id, active_value="Growth", tenant_id=tenant, proxy_user_id=owner,
        subject_key=GENERIC_CLAIM_SUBJECT)
    memory = Row(id=memory_id, is_archived=False, tenant_id=tenant, proxy_user_id=owner, source_event_id=event)
    revision = Row(id=revision_id, claim_id=claim_id, memory_id=memory_id,
        source_event_id=event, asserted_value="Growth")
    return claim, memory, revision


def test_coherent_winner_is_deliverable():
    assert source_winner_matches(*coherent())


@pytest.mark.parametrize("index,field,value", [
    (0,"status","disputed"), (1,"is_archived",True),
    (0,"active_memory_id",None), (0,"winning_revision_id",None),
    (0,"subject_key","user"),
    (1,"proxy_user_id",uuid.uuid4()),
    (2,"claim_id",uuid.uuid4()), (2,"memory_id",uuid.uuid4()),
    (2,"source_event_id",uuid.uuid4()), (2,"asserted_value","Starter"),
])
def test_incoherent_or_disputed_claim_cannot_be_presented_as_current(index,field,value):
    rows=coherent()
    setattr(rows[index],field,value)
    assert not source_winner_matches(*rows)


@pytest.mark.asyncio
async def test_source_projection_uses_winner_and_withholds_dispute():
    claim,memory,revision=coherent()
    from unittest.mock import AsyncMock
    old_id=str(uuid.uuid4())
    candidate=Row(id=old_id, provenance={"writer_id":"billing"},semantic_score=0.9)
    results=iter([Row(all=lambda:[(Row(memory_id=old_id),claim)]),Row(scalars=lambda:[revision])])
    service=object.__new__(RetrieverService)
    service.session=Row(execute=AsyncMock(side_effect=lambda *a:next(results)))
    service._fetch_memories_by_ids=AsyncMock(return_value={str(memory.id):memory})
    service._memory_is_current=lambda m:True
    service._memory_to_result=lambda m,**k:Row(id=str(m.id))
    args=dict(tenant_id=str(claim.tenant_id),limit=10,categories=["fact"],agent_id=None,time_filter_days=None)
    projected=await service._project_source_claims([candidate],owner=str(claim.proxy_user_id),**args)
    assert [r.id for r in projected]==[str(memory.id)]
    claim.status="disputed"
    results=iter([Row(all=lambda:[(Row(memory_id=old_id),claim)]),Row(scalars=lambda:[revision])])
    service._fetch_memories_by_ids=AsyncMock(return_value={})
    projected=await service._project_source_claims([candidate],owner=str(claim.proxy_user_id),**args)
    assert projected==[]
    assert service.last_memory_visibility.state=="unavailable"
    assert service.last_is_degraded


@pytest.mark.asyncio
async def test_legacy_merged_identity_is_explicitly_unavailable_without_mutation():
    from unittest.mock import AsyncMock
    claim, memory, revision = coherent()
    claim.subject_key = "user"
    candidate = Row(id=str(memory.id), provenance={"writer_id": "billing"}, semantic_score=0.9)
    service = object.__new__(RetrieverService)
    service.session = Row(execute=AsyncMock(side_effect=[
        Row(all=lambda: [(Row(memory_id=memory.id), claim)]), Row(scalars=lambda: [revision])]))
    service._fetch_memories_by_ids = AsyncMock(return_value={str(memory.id): memory})
    service._memory_is_current = lambda _: True
    before = [vars(row).copy() for row in (claim, memory, revision)]
    result = await service._project_source_claims([candidate], owner=str(claim.proxy_user_id),
        tenant_id=str(claim.tenant_id), limit=10, categories=["fact"], agent_id=None, time_filter_days=None)
    assert result == []
    assert service.last_is_degraded
    assert service.last_memory_visibility.state == "unavailable"
    assert service.last_memory_visibility.scope == "affected_context"
    assert [vars(row) for row in (claim, memory, revision)] == before


def test_typed_preference_keeps_explicit_identity_contract():
    from api.services.preference_claim_service import preference_declaration, preference_identity
    claim, memory, revision = coherent()
    provenance = {"extraction_evidence": {"explicit_preference_v1": {
        "property_key": "examples.language", "value": "Python", "applicability": {"kind": "global"},
        "turn_index": 0, "quote": "Python"}}}
    memory.metadata_json = {"provenance": provenance}
    claim.subject_key = "user"
    claim.claim_fingerprint = preference_identity(preference_declaration(provenance)).fingerprint
    assert source_winner_matches(claim, memory, revision)
    claim.claim_fingerprint = "wrong"
    assert not source_winner_matches(claim, memory, revision)
