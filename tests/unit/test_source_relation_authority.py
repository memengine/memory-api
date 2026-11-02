"""Source decision/authority ordering; controlled model output, no model calls.

Ledger persistence is isolated here; ledger and projection have separate tests.
These tests do not establish semantic relation-classification accuracy.
"""
import copy
import json
import uuid
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from api.db.models import MemoryCategory, ProxyUser
from api.services.conflict_resolver import ConflictResolver, SourceMemoryContext
from tests.unit.test_conflict_resolver import (
    FakeSession, make_existing_memory, source_candidate, source_response,
)


def scenario(relation, incoming_priority, stored_priority):
    target = make_existing_memory()
    target.content = "Account Alpha uses INR as its billing currency."
    target.category = MemoryCategory.fact
    target.metadata_json = {"provenance": {
        "writer_id": str(uuid.uuid4()),
        "authority_rules": {"default_priority": stored_priority},
    }}
    before = copy.deepcopy(target.metadata_json)
    incoming, messages = source_candidate(
        "Account Beta uses EUR as its billing currency." if relation == "coexists" else
        "Account Alpha now uses EUR as its billing currency." if relation == "supersedes" else
        target.content
    )
    incoming.category = "fact"
    payload = source_response(str(target.id), relation=relation)
    model = MagicMock()
    model.complete_sync.return_value = SimpleNamespace(content=json.dumps(payload), total_tokens=20)
    proxy = ProxyUser(id=target.proxy_user_id, tenant_id=uuid.uuid4())
    session = FakeSession(target, proxy)
    resolver = ConflictResolver(session=session, qdrant_service=MagicMock(),
        embedder=lambda _: [0.1] * 3, llm_service=model, source_messages=messages,
        provenance_snapshot={"writer_id": str(uuid.uuid4()),
            "authority_rules": {"default_priority": incoming_priority}})
    resolver._record_claim_for_memory = MagicMock()  # decision-layer isolation
    resolver._authority_conflict_decision = MagicMock(wraps=resolver._authority_conflict_decision)
    resolver._is_equal_authority_cross_writer_conflict = MagicMock(
        wraps=resolver._is_equal_authority_cross_writer_conflict)
    return target, before, incoming, proxy, session, resolver, model


@pytest.mark.parametrize("relation", ["coexists", "duplicate"])
@pytest.mark.parametrize("incoming_priority,stored_priority", [(90, 20), (20, 90), (50, 50)])
def test_authority_cannot_turn_nonreplacement_relation_into_update_or_conflict(
    relation, incoming_priority, stored_priority,
):
    target, before, incoming, proxy, session, resolver, model = scenario(
        relation, incoming_priority, stored_priority)
    stored = resolver.check_and_store([incoming], user_id=str(target.user_id),
        tenant_id=str(proxy.tenant_id), proxy_user_id=str(proxy.id),
        source_context=SourceMemoryContext((target,), complete=True))

    assert not target.is_archived
    assert target.metadata_json == before
    assert not resolver.last_pending_candidates
    assert resolver.last_user_clarifications_queued == 0
    if relation == "coexists":
        assert [row.resolution for row in stored] == ["KEEP_BOTH"]
        new = session.memories[stored[0].id]
        assert new.content == incoming.content and new.previous_version_id is None
    else:
        assert stored == [] and len(session.memories) == 1
        resolver._record_claim_for_memory.assert_not_called()
    resolver._authority_conflict_decision.assert_not_called()
    resolver._is_equal_authority_cross_writer_conflict.assert_not_called()
    assert model.complete_sync.call_count == 1


@pytest.mark.parametrize("incoming_priority,stored_priority,expected", [
    (90, 20, "UPDATE"), (20, 90, "REJECT"), (50, 50, "PENDING"),
])
def test_real_replacement_still_obeys_authority(incoming_priority, stored_priority, expected):
    target, before, incoming, proxy, session, resolver, model = scenario(
        "supersedes", incoming_priority, stored_priority)
    stored = resolver.check_and_store([incoming], user_id=str(target.user_id),
        tenant_id=str(proxy.tenant_id), proxy_user_id=str(proxy.id),
        source_context=SourceMemoryContext((target,), complete=True))
    resolver._authority_conflict_decision.assert_called_once()
    if expected == "UPDATE":
        assert target.is_archived
        assert [row.resolution for row in stored] == ["UPDATE"]
        assert session.memories[stored[0].id].previous_version_id == target.id
    else:
        assert not target.is_archived and target.metadata_json == before and stored == []
        assert len(resolver.last_pending_candidates) == (1 if expected == "PENDING" else 0)
    assert model.complete_sync.call_count == 1
