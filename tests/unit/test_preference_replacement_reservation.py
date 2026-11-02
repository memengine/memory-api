"""Synthetic refused-replacement regressions, no private cases or providers."""
import copy
import uuid
from types import SimpleNamespace as NS
from unittest.mock import Mock

import pytest
from sqlalchemy import Select
from sqlalchemy.dialects import postgresql
from sqlalchemy.sql.dml import Insert

from api.db.models import Memory, MemoryCategory, MemoryClaim, ProxyUser
from api.schemas.preferences import PreferenceClaimRequest
from api.services.conflict_resolver import ConflictResolver
from api.services.preference_claim_service import declared_preferences, preference_identity
from api.services.source_review_service import target_version


class Result:
    def __init__(self, row):
        self.row = row

    def scalar_one(self):
        assert self.row is not None
        return self.row

    def scalar_one_or_none(self):
        return self.row


class ClaimSession:
    """Apply the reservation INSERT only; record real SQL query/lock order."""
    def __init__(self, tenant, owner, ledger=None, target=None):
        self.tenant, self.owner = tenant, owner
        self.ledger, self.target = ledger, target
        self.statements = []
        self.commits = 0

    def get(self, model, key):
        assert model is ProxyUser and key == self.owner
        return NS(id=self.owner, tenant_id=self.tenant, is_blocked=False)

    def execute(self, statement):
        self.statements.append(statement)
        if isinstance(statement, Insert):
            assert statement.table.name == 'memory_claims'
            if self.ledger is None:
                self.ledger = NS(active_memory_id=None, active_value=None, winning_revision_id=None,
                                 status='disputed', authority_priority=20)
            return Result(None)
        assert isinstance(statement, Select)
        model = statement.column_descriptions[0]['entity']
        return Result(self.ledger if model is MemoryClaim else self.target)

    def commit(self):
        self.commits += 1


def declaration(**changes):
    return PreferenceClaimRequest.model_validate({
        'property_key': 'examples.language', 'value': 'Rust', 'quote': 'Use Rust for examples.',
        'applicability': {'kind': 'global'}, 'turn_index': 0, **changes})


def writer(session, claim):
    # Avoid constructing provider/router dependencies: run the real writer and
    # real evidence/claim policy with a deterministic synthetic embedder.
    resolver = ConflictResolver.__new__(ConflictResolver)
    resolver.session = session
    resolver.source_messages = [{'role': 'user', 'content': claim.quote}]
    resolver.embedder = Mock(return_value=[0.0] * 1536)
    resolver._confirmed_preference_command = None
    resolver.last_pending_candidates = []
    resolver.last_preference_choices_rejected = {}
    resolver.last_preference_choices_reaffirmed = 0
    resolver.last_preference_choices_withheld = 0
    resolver._store_new_memory = Mock(return_value=NS(id=str(uuid.uuid4())))
    resolver._archive_memory = Mock()
    resolver._create_audit_log = Mock()
    return resolver


def apply(resolver, claim):
    return resolver._store_declared_preferences(
        declared_preferences([claim], resolver.source_messages), user_id=str(uuid.uuid4()),
        tenant_id=str(resolver.session.tenant), proxy_user_id=str(resolver.session.owner),
        source_conversation_id=str(uuid.uuid4()), agent_id=None, auto_commit=True)


@pytest.mark.parametrize('operation', ['replacement', 'withhold'])
@pytest.mark.parametrize('scope', [{'kind': 'global'}, {'kind': 'context', 'namespace': 'project', 'key': 'Cedar'}])
def test_refused_first_target_command_does_not_persist_empty_claim(operation, scope):
    claim = declaration(commitment='replacement' if operation == 'replacement' else 'uncertain',
                        choice_state='candidate_only' if operation == 'replacement' else 'current_choice_unresolved',
                        replaces_memory_id=str(uuid.uuid4()), replaces_version='a' * 64,
                        applicability=scope)
    session = ClaimSession(uuid.uuid4(), uuid.uuid4())
    resolver = writer(session, claim)
    for _ in range(2):
        assert apply(resolver, claim) == []
        assert session.ledger is None  # The previous implementation inserted here.
    assert session.commits == 2  # Refusal outcome still completes normally.
    assert not any(isinstance(stmt, Insert) for stmt in session.statements)
    assert all(stmt.column_descriptions[0]['entity'] is MemoryClaim for stmt in session.statements)
    resolver._store_new_memory.assert_not_called()
    resolver._archive_memory.assert_not_called()
    if operation == 'replacement':
        assert resolver.last_pending_candidates
        evidence = resolver.last_pending_candidates[0].validated_evidence
        assert evidence['source_review']['target_memory_id'] is None
        assert evidence['source_review']['target_version'] is None
        assert evidence['source_decision']['reason_codes'] == ['preference_target_stale']
    else:
        assert resolver.last_preference_choices_rejected == {'preference_target_stale': 2}
        assert not resolver.last_pending_candidates
    for stmt in session.statements:
        sql = str(stmt.compile(dialect=postgresql.dialect()))
        assert 'FOR UPDATE' in sql
        values = list(stmt.compile(dialect=postgresql.dialect()).params.values())
        assert session.tenant in values and session.owner in values
        assert preference_identity(claim).fingerprint in values


@pytest.mark.parametrize('damage', ['wrong_target', 'stale_version', 'authority'])
def test_existing_ledger_rejection_keeps_winner_and_lock_order(damage):
    original = declaration()
    target = Memory(id=uuid.uuid4(), proxy_user_id=uuid.uuid4(), user_id=uuid.uuid4(),
                    content=original.quote, category=MemoryCategory.preference, is_archived=False,
                    metadata_json={'provenance': {'authority_rules': {'default_priority': 20}, 'extraction_evidence': {
                        'explicit_preference_v1': original.model_dump(mode='json')}}})
    tenant = uuid.uuid4()
    ledger = NS(active_memory_id=target.id, active_value=original.value, status='active',
                authority_priority=100 if damage == 'authority' else 20,
                winning_revision_id=uuid.uuid4())
    before = copy.deepcopy(vars(ledger))
    claim = declaration(commitment='replacement',
        replaces_memory_id=str(uuid.uuid4() if damage == 'wrong_target' else target.id),
        replaces_version='f' * 64 if damage == 'stale_version' else target_version(target))
    session = ClaimSession(tenant, target.proxy_user_id, ledger, target)
    resolver = writer(session, claim)
    assert apply(resolver, claim) == []
    assert vars(ledger) == before
    queries = [stmt for stmt in session.statements if isinstance(stmt, Select)]
    assert [stmt.column_descriptions[0]['entity'] for stmt in queries] == [MemoryClaim, Memory]
    assert all(stmt._for_update_arg is not None for stmt in queries)
    assert not any(isinstance(stmt, Insert) for stmt in session.statements)
    resolver._archive_memory.assert_not_called()
    resolver._store_new_memory.assert_not_called()
    assert resolver.last_pending_candidates


def test_new_store_retains_first_writer_upsert_and_applies():
    claim = declaration()
    session = ClaimSession(uuid.uuid4(), uuid.uuid4())
    resolver = writer(session, claim)
    assert len(apply(resolver, claim)) == 1
    assert isinstance(session.statements[0], Insert)
    assert 'ON CONFLICT ON CONSTRAINT uq_memory_claims_tenant_user_fingerprint DO NOTHING' in str(
        session.statements[0].compile(dialect=postgresql.dialect()))
    assert session.statements[1]._for_update_arg is not None
    resolver._store_new_memory.assert_called_once()
    assert not resolver.last_pending_candidates


@pytest.mark.parametrize('archived', [False, True])
def test_valid_replacement_still_uses_existing_owned_winner(archived):
    original = declaration()
    target = Memory(id=uuid.uuid4(), proxy_user_id=uuid.uuid4(), user_id=uuid.uuid4(),
                    content=original.quote, category=MemoryCategory.preference, is_archived=archived,
                    metadata_json={'provenance': {'authority_rules': {'default_priority': 20}, 'extraction_evidence': {
                        'explicit_preference_v1': original.model_dump(mode='json')}}})
    ledger = NS(active_memory_id=target.id, active_value=original.value,
                status='archived' if archived else 'active', authority_priority=20)
    claim = declaration(value='Go', quote='Use Go for examples.', commitment='replacement',
                        replaces_memory_id=str(target.id), replaces_version=target_version(target))
    resolver = writer(ClaimSession(uuid.uuid4(), target.proxy_user_id, ledger, target), claim)
    result = apply(resolver, claim)
    assert len(result) == 1 and not resolver.last_pending_candidates
    assert not any(isinstance(stmt, Insert) for stmt in resolver.session.statements)
    assert resolver._store_new_memory.call_args.kwargs['previous_version_id'] == str(target.id)
    assert resolver._store_new_memory.call_args.kwargs['resolution'] == 'UPDATE'
    assert resolver._archive_memory.call_count == (0 if archived else 1)


def test_valid_withhold_retains_current_target_and_records_choice_state():
    original = declaration()
    target = Memory(id=uuid.uuid4(), proxy_user_id=uuid.uuid4(), user_id=uuid.uuid4(),
                    content=original.quote, category=MemoryCategory.preference, is_archived=False,
                    metadata_json={'provenance': {'authority_rules': {'default_priority': 20},
                        'extraction_evidence': {'explicit_preference_v1': original.model_dump(mode='json')}}})
    ledger = NS(active_memory_id=target.id, active_value=original.value, status='active',
                authority_priority=20, proxy_user_id=target.proxy_user_id,
                claim_fingerprint=preference_identity(original).fingerprint)
    claim = declaration(commitment='uncertain', choice_state='current_choice_unresolved',
                        replaces_memory_id=str(target.id), replaces_version=target_version(target))
    resolver = writer(ClaimSession(uuid.uuid4(), target.proxy_user_id, ledger, target), claim)
    assert apply(resolver, claim) == []
    assert ledger.active_memory_id == target.id and ledger.status == 'disputed'
    assert resolver.last_preference_choices_withheld == 1
    assert target.metadata_json['preference_choice_state']['state'] == 'current_choice_unresolved'
    assert not any(isinstance(stmt, Insert) for stmt in resolver.session.statements)
    resolver._store_new_memory.assert_not_called()
    assert resolver.last_pending_candidates


def test_preexisting_empty_ledger_is_not_deleted_or_recreated_on_refusal():
    ledger = NS(active_memory_id=None, active_value=None, winning_revision_id=None,
                status='disputed', authority_priority=20)
    before = copy.deepcopy(vars(ledger))
    claim = declaration(commitment='replacement', replaces_memory_id=str(uuid.uuid4()),
                        replaces_version='b' * 64)
    session = ClaimSession(uuid.uuid4(), uuid.uuid4(), ledger)
    resolver = writer(session, claim)
    assert apply(resolver, claim) == []
    assert session.ledger is ledger and vars(ledger) == before
    assert len(session.statements) == 1 and isinstance(session.statements[0], Select)
    assert resolver.last_pending_candidates
