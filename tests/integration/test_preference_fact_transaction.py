"""Isolated PostgreSQL lifecycle tests; controlled semantics, never provider eval."""
import json
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from threading import Barrier

import pytest
from sqlalchemy import create_engine, event, select
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import sessionmaker

from api.db.models import ExtractionJob, Memory, MemoryClaim
from api.services.preference_fact_transaction import PreferenceFactTransactionService
from api.services.preference_intent_admission_service import (
    PreferenceAdmissionUnavailable,
)
from api.services.preference_intent_transaction import (
    PreferenceIntentTransactionService,
)
from api.services.source_review_service import target_version
from api.tasks.extraction_tasks import (
    _create_source_conversation,
    _ensure_proxy_backing_user,
)
from tests.integration.test_preference_choice_operations import counts, read
from tests.integration.test_preference_intent_admission import args, job
from tests.integration.test_preference_intent_transaction import (
    FACT,
    TEXT,
    pending,
)
from tests.integration.test_preference_intent_transaction import (
    forbid_real_providers as _forbid_real_providers,
)
from tests.integration.test_preference_snapshots import identity, seed
from tests.integration.test_source_decision_postgres import (
    _decision,
    _embedding,
    _Model,
)
from tests.integration.test_source_decision_postgres import (
    sql_scope as _sql_scope,
)
from tests.unit.test_preference_intent_contract import reply

sql_scope = _sql_scope
forbid_real_providers = _forbid_real_providers


class Model:
    def __init__(self, responses, check=None):
        self.responses = iter(responses)
        self.check = check
        self.calls = []

    def complete_sync(self, **kwargs):
        if self.check:
            self.check()
        prompt = json.loads(kwargs["user_message"])
        self.calls.append(prompt)
        response = next(self.responses)
        return _Model._response(response(prompt) if callable(response) else response)


def fact(scope, content="my exam is on October 10", **changes):
    with scope.factory.begin() as session:
        backing = _ensure_proxy_backing_user(session, str(scope.proxy_id))
        conversation = _create_source_conversation(session, user_id=backing.id, agent_id=None, message_count=1)
        row = Memory(id=uuid.uuid4(), user_id=backing.id, proxy_user_id=scope.proxy_id,
            source_conversation_id=conversation.id, content=content, category="fact", importance_score=5,
            confidence_score=0.5, embedding_id=str(uuid.uuid4()), embedding_model_id=_embedding(content).model_id,
            metadata_json={"provenance": {"attestation": "client_asserted", "authority_priority": 20,
                                         "authority_rules": {"default_priority": 20}}}, **changes)
        session.add(row)
        return row.id


def prepare(scope, responses, raw=None, text=TEXT, check=None):
    service = PreferenceFactTransactionService(scope.factory)
    job_id = job(scope, messages=[{"role": "user", "content": text}], external_conversation_id="mixed-fact-test")
    captured = service.capture(**args(scope, job_id))
    model = Model(responses, check)
    record = service.admit(raw or reply(nonpreferences=[FACT]), llm_service=model,
        **args(scope, job_id), capture_id=captured.capture_id)
    return service, job_id, record, model


def apply(scope, service, job_id, record):
    texts = [p.quote for p in record.reply.preferences if p.action in {"assert", "set_current"}]
    texts += [f.content for f in record.reply.nonpreferences]
    return service.apply(**args(scope, job_id), capture_id=record.capture_id,
                         embeddings={t: _embedding(t) for t in texts})


def stored(scope, job_id):
    with scope.factory() as session:
        return PreferenceFactTransactionService._read(session.get(ExtractionJob, job_id))


def test_mixed_corrections_use_existing_writer_and_stable_receipt(sql_scope, monkeypatch):
    preference_id, fact_id = seed(sql_scope, monkeypatch), fact(sql_scope)
    service, job_id, record, model = prepare(sql_scope, [_decision(fact_id, relation="supersedes")])
    result = apply(sql_scope, service, job_id, record)
    assert result.receipt.facts_created == 1 and len(result.receipt.created_memory_ids) == 2
    assert result.receipt.deferred_facts == result.receipt.facts_rejected == 0
    assert identity(read(sql_scope)).target.value == "Python"
    with sql_scope.factory() as session:
        assert session.get(Memory, preference_id).is_archived and session.get(Memory, fact_id).is_archived
        rows = [session.get(Memory, mid) for mid in result.receipt.created_memory_ids]
        assert {r.category.value for r in rows} == {"fact", "preference"}
        assert all(r.metadata_json["provenance"]["attestation"] == "client_asserted" for r in rows)
        assert all(r.metadata_json["provenance"]["external_conversation_id"] == "mixed-fact-test" for r in rows)
    assert pending(sql_scope, job_id) == []
    before = counts(sql_scope)
    restarted = PreferenceFactTransactionService(sql_scope.factory)
    assert restarted.apply(**args(sql_scope, job_id), capture_id=record.capture_id) == result == stored(sql_scope, job_id)
    assert restarted.admit(record.reply, llm_service=model, **args(sql_scope, job_id), capture_id=record.capture_id) == result
    assert counts(sql_scope) == before and len(model.calls) == 1
    with sql_scope.factory() as session, pytest.raises(PreferenceAdmissionUnavailable, match="record_inconsistent"):
        PreferenceIntentTransactionService._read(session.get(ExtractionJob, job_id))


def test_provider_has_no_checked_out_connection_even_with_one_slot_and_expiration(sql_scope):
    engine = create_engine(sql_scope.factory.kw["bind"].url, pool_size=1, max_overflow=0, pool_timeout=2)
    active = set()
    def checkout(_connection, record, _proxy):
        active.add(id(record))
    def checkin(_connection, record):
        active.discard(id(record))
    event.listen(engine, "checkout", checkout)
    event.listen(engine, "checkin", checkin)
    try:
        sql_scope.factory = sessionmaker(engine, expire_on_commit=True)
        def check():
            assert not active
        service, job_id, record, _ = prepare(sql_scope, [_decision()], check=check)
        assert apply(sql_scope, service, job_id, record).receipt.facts_created == 1
    finally:
        event.remove(engine, "checkout", checkout)
        event.remove(engine, "checkin", checkin)
        engine.dispose()


@pytest.mark.parametrize("change", ["content", "metadata", "expiry", "new_fact"])
def test_changed_context_never_rebinds_and_preserves_preference_write(sql_scope, monkeypatch, change):
    seed(sql_scope, monkeypatch)
    fact_id = fact(sql_scope)
    service, job_id, record, _ = prepare(sql_scope, [_decision(fact_id, relation="supersedes")])
    if change == "new_fact":
        fact(sql_scope, "my exam is on November 2")
    else:
        with sql_scope.factory.begin() as session:
            row = session.get(Memory, fact_id)
            if change == "content":
                row.content = "my exam is on November 2"
            elif change == "metadata":
                row.metadata_json = {**row.metadata_json, "edited": True}
            else:
                row.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    result = apply(sql_scope, service, job_id, record)
    assert result.receipt.facts_created == 0 and result.receipt.deferred_facts == 1
    assert len(result.receipt.created_memory_ids) == 1 and identity(read(sql_scope)).target.value == "Python"
    assert len(pending(sql_scope, job_id)) == 1
    with sql_scope.factory() as session:
        assert not session.get(Memory, fact_id).is_archived


def test_failure_after_both_writers_rolls_back_all_effects(sql_scope, monkeypatch):
    seed(sql_scope, monkeypatch)
    fact_id = fact(sql_scope)
    service, job_id, record, _ = prepare(sql_scope, [_decision(fact_id, relation="supersedes")])
    before = counts(sql_scope), read(sql_scope)
    original = service._store_nonpreferences
    def crash(*args, **kwargs):
        original(*args, **kwargs)
        raise RuntimeError("after fact write")
    with monkeypatch.context() as patch:
        patch.setattr(service, "_store_nonpreferences", crash)
        with pytest.raises(RuntimeError, match="after fact write"):
            apply(sql_scope, service, job_id, record)
    assert stored(sql_scope, job_id) == record and (counts(sql_scope), read(sql_scope)) == before
    with sql_scope.factory() as session:
        assert not session.get(Memory, fact_id).is_archived
    assert apply(sql_scope, service, job_id, record).receipt.facts_created == 1


def test_unrelated_new_facts_are_preserved_and_batch_dependencies_are_visible(sql_scope):
    findings = [{"content": t, "quote": t, "category": "fact", "turn_index": 0}
                for t in ("my exam is on October 18", "my office is in Pune")]
    service, job_id, record, model = prepare(sql_scope, [_decision(), _decision()],
        reply([], findings), text=". ".join(f["quote"] for f in findings))
    assert model.calls[0]["existing_candidates"] == []
    assert model.calls[1]["existing_candidates"][0]["content"] == findings[0]["content"]
    result = apply(sql_scope, service, job_id, record)
    assert result.receipt.facts_created == len(result.receipt.created_memory_ids) == 2


@pytest.mark.parametrize("target_kind", ["transient", "same_existing"])
def test_conflicting_facts_in_same_batch_never_both_activate(sql_scope, target_kind):
    first_id = fact(sql_scope) if target_kind == "same_existing" else None
    findings = [{"content": t, "quote": t, "category": "fact", "turn_index": 0}
                for t in ("my exam is on October 18", "my exam is on October 20")]
    def second(prompt):
        selected = first_id or next(c["id"] for c in prompt["existing_candidates"] if c["content"] == findings[0]["content"])
        return _decision(selected, relation="supersedes")
    responses = [_decision(first_id, relation="supersedes" if first_id else "novel"), second]
    service, job_id, record, _ = prepare(sql_scope, responses, reply([], findings),
        text=". ".join(f["quote"] for f in findings))
    assert all(p.action == "pending" for p in record.fact_plans)
    result = apply(sql_scope, service, job_id, record)
    assert result.receipt.facts_created == 0 and result.receipt.deferred_facts == 2
    assert len(pending(sql_scope, job_id)) == 2


@pytest.mark.parametrize("kind", ["protected_preference", "mislabeled_preference", "trusted_fact", "uncertain", "invalid"])
def test_no_fact_lane_bypass_or_untrusted_activation(sql_scope, monkeypatch, kind):
    target_id = seed(sql_scope, monkeypatch) if kind == "protected_preference" else fact(sql_scope)
    if kind == "mislabeled_preference":
        preference_id = seed(sql_scope, monkeypatch)
        with sql_scope.factory.begin() as session:
            session.get(Memory, target_id).metadata_json = session.get(Memory, preference_id).metadata_json
    if kind == "trusted_fact":
        with sql_scope.factory.begin() as session:
            row = session.get(Memory, target_id)
            row.metadata_json = {"provenance": {"attestation": "user_confirmed", "authority_rules": {"default_priority": 80}}}
    decision = _decision(target_id, relation="supersedes")
    if kind == "uncertain":
        decision = _decision(target_id, relation="ambiguous", state="unclear")
    if kind == "invalid":
        decision["selected_memory_id"] = str(uuid.uuid4())
    service, job_id, record, _ = prepare(sql_scope, [decision], reply([], [FACT]))
    result = apply(sql_scope, service, job_id, record)
    assert result.receipt.facts_created == 0 and result.receipt.deferred_facts == 1
    with sql_scope.factory() as session:
        assert not session.get(Memory, target_id).is_archived


def test_large_context_is_pending_without_provider_or_silent_truncation(sql_scope):
    for i in range(51):
        fact(sql_scope, f"old fact {i}")
    service, job_id, record, model = prepare(sql_scope, [], reply([], [FACT]))
    assert not record.fact_context_complete and len(record.fact_context) == 50
    assert model.calls == [] and record.fact_plans[0].action == "pending"
    assert apply(sql_scope, service, job_id, record).receipt.deferred_facts == 1


def test_foreign_caller_and_changed_binding_cannot_read_or_apply(sql_scope):
    fact_id = fact(sql_scope)
    service, job_id, record, _ = prepare(sql_scope, [_decision(fact_id, relation="supersedes")])
    with pytest.raises(PreferenceAdmissionUnavailable):
        service.apply(**{**args(sql_scope, job_id), "proxy_user_id": uuid.uuid4()}, capture_id=record.capture_id)
    with sql_scope.factory() as session:
        assert record.fact_context[0].version == target_version(session.get(Memory, fact_id))


def test_concurrent_corrections_retry_fixed_plans_not_new_classification(sql_scope, monkeypatch):
    target_id = fact(sql_scope)
    cases = []
    for day in (18, 20):
        content = f"my exam moved to October {day}"
        finding = {**FACT, "content": content, "quote": content}
        cases.append(prepare(sql_scope, [_decision(target_id, relation="supersedes")],
                             reply([], [finding]), text=content))
    barrier = Barrier(2, timeout=5)
    original = PreferenceFactTransactionService._prepare_nonpreferences
    def synchronized(*args):
        context = original(*args)
        barrier.wait()
        return context
    def run(item):
        service, job_id, record, _ = item
        try:
            return apply(sql_scope, service, job_id, record)
        except DBAPIError as error:
            assert error.orig.pgcode == "40001"  # Expected whole-transaction retry, not rebinding.
            return None
    with monkeypatch.context() as patch:
        patch.setattr(PreferenceFactTransactionService, "_prepare_nonpreferences", synchronized)
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(run, cases))
    assert sum(r.receipt.facts_created for r in results if r is not None) == 1
    for index, result in enumerate(results):
        if result is None:
            service, job_id, record, model = cases[index]
            assert stored(sql_scope, job_id) == record
            results[index] = apply(sql_scope, service, job_id, record)
            assert len(model.calls) == 1
    assert sorted(r.receipt.facts_created for r in results) == [0, 1]
    assert sorted(r.receipt.deferred_facts for r in results) == [0, 1]


def test_change_during_classification_is_pending_and_does_not_refresh(sql_scope):
    target_id = fact(sql_scope)
    def change():
        with sql_scope.factory.begin() as session:
            session.get(Memory, target_id).content = "my exam is on November 2"
    service, job_id, record, model = prepare(sql_scope, [_decision(target_id, relation="supersedes")],
                                           reply([], [FACT]), check=change)
    assert record.fact_plans[0].action == "UPDATE"
    result = apply(sql_scope, service, job_id, record)
    assert result.receipt.deferred_facts == 1 and result.receipt.facts_created == 0
    assert len(model.calls) == 1


def test_pending_fact_needs_no_embedding_and_duplicate_pending_is_not_counted_twice(sql_scope):
    findings = [{**FACT, "content": t, "quote": t} for t in ("my exam might be in October", "my office might be in Pune")]
    service, job_id, record, _ = prepare(sql_scope, [{}, {}], reply([], findings),
                                        text=". ".join(f["quote"] for f in findings))
    result = service.apply(**args(sql_scope, job_id), capture_id=record.capture_id)
    assert result.receipt.deferred_facts == result.receipt.pending_candidates_buffered == 2
    assert len(pending(sql_scope, job_id)) == 2


def test_active_ledger_authority_alone_cannot_be_bypassed(sql_scope):
    initial = "my exam is on October 10"
    finding = {**FACT, "content": initial, "quote": initial}
    service, first_id, first, _ = prepare(sql_scope, [_decision()], reply([], [finding]), text=initial)
    target_id = apply(sql_scope, service, first_id, first).receipt.created_memory_ids[0]
    with sql_scope.factory.begin() as session:
        claim = session.scalars(select(MemoryClaim).where(MemoryClaim.active_memory_id == target_id)).one()
        assert claim.authority_priority == 20
        claim.authority_priority = 80
    service, job_id, record, _ = prepare(sql_scope, [_decision(target_id, relation="supersedes")], reply([], [FACT]))
    assert record.fact_plans[0].action == "UPDATE"  # Provenance is unchanged; ledger is the veto.
    result = apply(sql_scope, service, job_id, record)
    assert result.receipt.deferred_facts == 1 and result.receipt.facts_created == 0
    with sql_scope.factory() as session:
        assert not session.get(Memory, target_id).is_archived


def test_duplicate_fact_is_reported_as_rejected_not_created_or_pending(sql_scope):
    content = "my exam is on October 10"
    target_id = fact(sql_scope, content)
    finding = {**FACT, "content": content, "quote": content}
    service, job_id, record, _ = prepare(sql_scope, [_decision(target_id, relation="duplicate")],
                                        reply([], [finding]), text=content)
    result = apply(sql_scope, service, job_id, record)
    assert result.receipt.facts_rejected == 1 and result.receipt.facts_created == result.receipt.deferred_facts == 0
    assert pending(sql_scope, job_id) == []
