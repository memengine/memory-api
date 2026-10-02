"""Backend containment tests, not evidence of model scope-classification accuracy."""

from __future__ import annotations

import copy
import json
import uuid
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from api.db.models import MemoryCategory, ProxyUser
from api.services.conflict_resolver import ConflictResolver, SourceMemoryContext
from api.services.source_review_service import review_intent
from tests.unit.test_conflict_resolver import (
    FakeSession,
    make_existing_memory,
    source_candidate,
    source_response,
)


@pytest.mark.parametrize(
    "content",
    [
        "For the synthetic Release Check project only, my default language for programming examples is C++",
        "सिर्फ परीक्षा प्रोजेक्ट के उदाहरणों के लिए मेरी डिफ़ॉल्ट भाषा C++ है।",
        "Sirf exam project ke examples ke liye meri default language C++ hai.",
    ],
)
@pytest.mark.parametrize(
    "incoming_priority,stored_priority", [(20, 90), (50, 50), (90, 20)]
)
@pytest.mark.parametrize("merge_preserves_text", [False, True])
def test_source_merge_stays_pending_before_authority_or_any_memory_write(
    content,
    incoming_priority,
    stored_priority,
    merge_preserves_text,
):
    target = make_existing_memory()
    target.content = "My default language for every programming example is C++."
    target.category = MemoryCategory.preference
    target.metadata_json = {
        "provenance": {"authority_rules": {"default_priority": stored_priority}}
    }
    original = copy.deepcopy(target.metadata_json)
    incoming, messages = source_candidate(content)
    incoming.category = "preference"
    evidence = copy.deepcopy(incoming.validated_evidence)
    payload = source_response(str(target.id), relation="mergeable")
    payload["merged_memory"] = {
        "content": content if merge_preserves_text else target.content,
        "category": "preference",
        "importance_score": 8,
        "confidence": 0.99,
        "expiry": "permanent",
        "reasoning": "Both preferences use C++, including a particular project context.",
    }
    model = MagicMock()
    model.complete_sync.return_value = SimpleNamespace(
        content=json.dumps(payload), total_tokens=20
    )
    proxy = ProxyUser(id=target.proxy_user_id, tenant_id=uuid.uuid4())
    session = FakeSession(target, proxy)
    resolver = ConflictResolver(
        session=session,
        qdrant_service=MagicMock(),
        embedder=lambda _: [0.1] * 3,
        llm_service=model,
        source_messages=messages,
        provenance_snapshot={
            "authority_rules": {"default_priority": incoming_priority}
        },
    )
    # Verify the real authority policy would have allowed the bypass, not only
    # a mocked assumption about higher-priority input.
    authority = resolver._authority_conflict_decision(incoming, target)
    if incoming_priority > stored_priority:
        assert authority.action == "UPDATE"
    elif incoming_priority < stored_priority:
        assert authority.action == "REJECT"
    resolver._authority_conflict_decision = MagicMock(
        wraps=resolver._authority_conflict_decision
    )
    resolver._archive_memory = MagicMock(wraps=resolver._archive_memory)
    resolver._store_new_memory_with_shared_context = MagicMock(
        wraps=resolver._store_new_memory_with_shared_context
    )

    assert (
        resolver.check_and_store(
            [incoming],
            user_id=str(target.user_id),
            tenant_id=str(proxy.tenant_id),
            proxy_user_id=str(proxy.id),
            source_context=SourceMemoryContext((target,), complete=True),
        )
        == []
    )
    resolver._authority_conflict_decision.assert_not_called()
    resolver._archive_memory.assert_not_called()
    resolver._store_new_memory_with_shared_context.assert_not_called()
    assert target.content == "My default language for every programming example is C++."
    assert not target.is_archived and target.metadata_json == original
    assert session.added == [] and len(session.memories) == 1
    assert resolver.last_user_clarifications_queued == 0
    assert model.complete_sync.call_count == 1
    (pending,) = resolver.last_pending_candidates
    assert (
        pending.content == content
        and pending.candidate_reason == "source_decision_pending"
    )
    assert pending.validated_evidence["source_review"] == review_intent(target)
    for key, value in evidence.items():
        assert pending.validated_evidence[key] == value
    audit = pending.validated_evidence["source_decision"]
    assert "source_merge_containment" in audit["reason_codes"]
    assert audit["details"]["proposed_action"] == "MERGE"
    assert incoming.validated_evidence == evidence


def test_stale_merge_target_does_not_receive_a_containment_review():
    target = make_existing_memory()
    proxy = ProxyUser(id=target.proxy_user_id, tenant_id=uuid.uuid4())
    incoming, messages = source_candidate("For project examples only, I use Go.")
    payload = source_response(str(target.id), relation="mergeable")
    payload["merged_memory"] = {
        "content": incoming.content,
        "category": "expertise",
        "importance_score": 8,
        "confidence": 0.99,
        "expiry": "permanent",
        "reasoning": "Controlled merge.",
    }

    def concurrent_change(**_kwargs):
        target.content = "A concurrently changed preference."
        return SimpleNamespace(content=json.dumps(payload), total_tokens=20)

    model = MagicMock()
    model.complete_sync.side_effect = concurrent_change
    resolver = ConflictResolver(
        session=FakeSession(target, proxy),
        qdrant_service=MagicMock(),
        embedder=lambda _: [0.1] * 3,
        llm_service=model,
        source_messages=messages,
    )
    assert (
        resolver.check_and_store(
            [incoming],
            user_id=str(target.user_id),
            tenant_id=str(proxy.tenant_id),
            proxy_user_id=str(proxy.id),
            source_context=SourceMemoryContext((target,), complete=True),
        )
        == []
    )
    (pending,) = resolver.last_pending_candidates
    assert (
        "source_target_stale"
        in pending.validated_evidence["source_decision"]["reason_codes"]
    )
    assert "source_review" not in pending.validated_evidence
    assert (
        target.content == "A concurrently changed preference."
        and not target.is_archived
    )
