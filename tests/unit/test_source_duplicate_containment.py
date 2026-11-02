"""A model-only duplicate decision cannot silently discard another proposition."""
import pytest

from api.services.conflict_resolver import SourceMemoryContext
from api.services.source_review_service import is_source_review, review_intent
from tests.unit.test_source_relation_authority import scenario


@pytest.mark.parametrize("difference", ["attribute", "paraphrase", "case", "scope", "category", "expiry", "validity"])
def test_unverified_duplicate_becomes_nonactivating_review(difference):
    target, before, incoming, proxy, session, resolver, model = scenario("duplicate", 90, 20)
    if difference == "attribute":
        incoming.content = "Account Alpha has subscription plan Growth."
        # Rebuild the verified evidence, not a fabricated grounding flag.
        from tests.unit.test_conflict_resolver import source_candidate
        incoming, resolver.source_messages = source_candidate(incoming.content)
        incoming.category = "fact"
    elif difference == "paraphrase":
        from tests.unit.test_conflict_resolver import source_candidate
        incoming, resolver.source_messages = source_candidate("The billing currency for Account Alpha is INR.")
        incoming.category = "fact"
    else:
        from tests.unit.test_conflict_resolver import source_candidate
        text = target.content.lower() if difference == "case" else target.content
        incoming, resolver.source_messages = source_candidate(text)
        incoming.category = "fact"
        if difference == "scope": resolver.provenance_snapshot["scope"] = {"project": "other"}
        if difference == "category": incoming.category = "preference"
        if difference == "expiry": incoming.expiry = "temporary"
        if difference == "validity":
            from datetime import UTC, datetime
            target.effective_from = datetime(2026, 1, 1, tzinfo=UTC)
    stored = resolver.check_and_store([incoming], user_id=str(target.user_id),
        tenant_id=str(proxy.tenant_id), proxy_user_id=str(proxy.id),
        source_context=SourceMemoryContext((target,), complete=True))
    assert stored == [] and not target.is_archived and target.metadata_json == before
    assert len(session.memories) == 1
    resolver._record_claim_for_memory.assert_not_called()
    resolver._authority_conflict_decision.assert_not_called()
    (pending,) = resolver.last_pending_candidates
    if difference == "scope":
        # The target is now excluded before classification; the controlled
        # model's attempt to name it is rejected as an invalid nomination.
        assert "source_target_invalid" in pending.validated_evidence["source_decision"]["reason_codes"]
        assert pending.content == incoming.content
        assert not resolver._exact_source_duplicate(incoming, target)
        return
    assert pending.content == incoming.content and is_source_review(pending)
    assert pending.validated_evidence["source_review"] == review_intent(target)
    assert "source_duplicate_unverified" in pending.validated_evidence["source_decision"]["reason_codes"]
    assert pending.validated_evidence["source_spans"] == incoming.validated_evidence["source_spans"]
    assert model.complete_sync.call_count == 1


def test_duplicate_without_target_is_retained_for_review():
    import json
    from types import SimpleNamespace
    from tests.unit.test_conflict_resolver import source_response
    target, before, incoming, proxy, session, resolver, model = scenario("duplicate", 90, 20)
    model.complete_sync.return_value = SimpleNamespace(content=json.dumps(source_response(relation="duplicate")), total_tokens=20)
    assert resolver.check_and_store([incoming], user_id=str(target.user_id), tenant_id=str(proxy.tenant_id),
        proxy_user_id=str(proxy.id), source_context=SourceMemoryContext((), complete=True)) == []
    (pending,) = resolver.last_pending_candidates
    assert is_source_review(pending)
    assert pending.validated_evidence["source_review"] == review_intent(None)
    assert "source_duplicate_unverified" in pending.validated_evidence["source_decision"]["reason_codes"]
    assert not target.is_archived and target.metadata_json == before
    resolver._authority_conflict_decision.assert_not_called()
    assert model.complete_sync.call_count == 1


def test_exact_duplicate_keeps_nonmutating_rejection():
    from tests.unit.test_conflict_resolver import source_candidate
    target, before, _, proxy, session, resolver, model = scenario("duplicate", 90, 20)
    incoming, resolver.source_messages = source_candidate(target.content)
    incoming.category = "fact"
    stored = resolver.check_and_store([incoming], user_id=str(target.user_id),
        tenant_id=str(proxy.tenant_id), proxy_user_id=str(proxy.id),
        source_context=SourceMemoryContext((target,), complete=True))
    assert stored == [] and not resolver.last_pending_candidates and not target.is_archived
    assert target.metadata_json == before and len(session.memories) == 1
    resolver._record_claim_for_memory.assert_not_called()
