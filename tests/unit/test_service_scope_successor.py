"""Registered-service candidate scope and unresolved successor containment."""
import copy
import uuid
from unittest.mock import MagicMock

import pytest

from api.db.models import ProxyUser
from api.services.conflict_resolver import ConflictResolver, SourceMemoryContext
from tests.unit.test_conflict_resolver import FakeSession, make_existing_memory, make_new_memory, make_llm_client, make_qdrant_point


def setup_service(*, stored_scope, incoming_scope, action="UPDATE", correction=False):
    target = make_existing_memory()
    target.content = "Implementation language for this project is TypeScript."
    writer = str(uuid.uuid4())
    target.metadata_json = {"provenance": {"writer_id": writer, "scope": stored_scope,
        "authority_rules": {"default_priority": 95}}}
    proxy = ProxyUser(id=target.proxy_user_id, tenant_id=uuid.uuid4())
    session = FakeSession(target, proxy)
    vector = MagicMock()
    vector.search_memories.return_value = [make_qdrant_point(target)]
    resolver = ConflictResolver(session=session, qdrant_service=vector,
        embedder=lambda _: [0.1]*3, client=make_llm_client(action),
        default_source_event_id=uuid.uuid4(), provenance_snapshot={"writer_id": writer,
            "scope": incoming_scope, "authority_rules": {"default_priority": 95}})
    resolver._record_claim_for_memory = MagicMock()  # SQL ledger tested separately
    incoming = make_new_memory("Implementation language for this project is Python.")
    incoming.validated_evidence = {"claim_state": "correction" if correction else "asserted"}
    return target, proxy, session, resolver, incoming


@pytest.mark.parametrize("stored_scope,incoming_scope", [
    ({"project": "alpha"}, {"project": "beta"}),
    ({}, {"project": "beta"}),
    ({"project": "alpha"}, {}),
])
def test_distinct_declared_scopes_are_not_replacement_candidates(stored_scope, incoming_scope):
    target, proxy, session, resolver, incoming = setup_service(stored_scope=stored_scope, incoming_scope=incoming_scope)
    before = copy.deepcopy(target.metadata_json)
    resolver._classify_conflict = MagicMock(wraps=resolver._classify_conflict)
    stored = resolver.check_and_store([incoming], user_id=str(target.user_id), tenant_id=str(proxy.tenant_id),
        proxy_user_id=str(proxy.id), source_context=SourceMemoryContext((target,), complete=True))
    assert not target.is_archived and target.metadata_json == before
    assert [s.resolution for s in stored] == ["NEW"]
    assert session.memories[stored[0].id].previous_version_id is None
    assert session.memories[stored[0].id].metadata_json["provenance"]["scope"] == incoming_scope
    resolver._classify_conflict.assert_not_called()


@pytest.mark.parametrize("nomination", [True, False])
def test_unresolved_explicit_correction_cannot_create_another_current_claim(nomination):
    target, proxy, session, resolver, incoming = setup_service(stored_scope={"project": "alpha"},
        incoming_scope={"project": "alpha"}, action="KEEP_BOTH", correction=True)
    if not nomination:
        resolver.qdrant_service.search_memories.return_value = []
    stored = resolver.check_and_store([incoming], user_id=str(target.user_id), tenant_id=str(proxy.tenant_id),
        proxy_user_id=str(proxy.id), source_context=SourceMemoryContext((target,), complete=True))
    assert stored == [] and not target.is_archived and len(session.memories) == 1
    resolver._record_claim_for_memory.assert_not_called()
    (pending,) = resolver.last_pending_candidates
    assert pending.candidate_reason == "source_decision_pending" and pending.content == incoming.content
    # Service events lack verified user spans; do not invent a user review card.
    assert "source_review" not in pending.validated_evidence
    assert "source_correction_unresolved" in pending.validated_evidence["source_decision"]["reason_codes"]


def test_same_scope_valid_update_still_replaces_only_selected_target():
    target, proxy, session, resolver, incoming = setup_service(stored_scope={"project": "alpha"},
        incoming_scope={"project": "alpha"}, correction=True)
    stored = resolver.check_and_store([incoming], user_id=str(target.user_id), tenant_id=str(proxy.tenant_id),
        proxy_user_id=str(proxy.id), source_context=SourceMemoryContext((target,), complete=True))
    assert [s.resolution for s in stored] == ["UPDATE"]
    assert target.is_archived and session.memories[stored[0].id].previous_version_id == target.id
