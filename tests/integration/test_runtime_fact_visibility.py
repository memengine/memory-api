"""Normal completed-job reads, not a classifier benchmark or an applied-state reset."""
import copy
import json
import uuid

import pytest
from sqlalchemy import event

from api.db.models import (
    AuditLog,
    ExtractionJob,
    ExtractionJobStatus,
    Memory,
    MemoryClaim,
    RuntimeJobReconciliation,
)
from api.services.runtime_visibility_backfill import backfill_visibility_batch
from api.settings import get_settings
from api.tasks import provenance_tasks
from tests.integration.test_preference_fact_transaction import fact
from tests.integration.test_preference_intent_transaction import FACT, TEXT
from tests.integration.test_runtime_pending_visibility import preference_context, read
from tests.integration.test_runtime_preference_ingestion import ingestion as _ingestion
from tests.integration.test_runtime_preference_worker import enable, seed, worker
from tests.integration.test_runtime_preference_worker import (
    forbid_real_providers as _forbid,
)
from tests.integration.test_runtime_preference_worker import sql_scope as _scope
from tests.integration.test_source_decision_postgres import _decision
from tests.unit.test_preference_intent_contract import reply

sql_scope, ingestion, forbid_real_providers = _scope, _ingestion, _forbid


def prepare(scope, ingestion, monkeypatch, *, large=False, mixed=False, authority=False):
    old_pref = seed(scope, monkeypatch) if mixed else None
    old = fact(scope)
    if authority:
        with scope.factory.begin() as session:
            row = session.get(Memory, old)
            row.metadata_json = {"provenance": {"attestation": "user_confirmed", "authority_priority": 90}}
    if large:
        for i in range(51):
            fact(scope, f"unrelated fact {i}")
    unrelated = fact(scope, "my city is Pune")
    enable(scope, monkeypatch)
    monkeypatch.setattr(get_settings(), "runtime_target_withholding_enabled", True)
    raw = reply(nonpreferences=[FACT]) if mixed else reply(preferences=[], nonpreferences=[FACT])
    new = uuid.UUID(ingestion.send(messages=[{"role": "user", "content": TEXT}])["job_id"])
    decisions = [] if large else [_decision(old, state="unclear", relation="ambiguous")]
    instance, model, _ = worker(scope, [raw, *decisions])
    result = instance.process(new)
    return old, unrelated, old_pref, new, instance, result, model


@pytest.mark.parametrize("large", [False, True])
def test_applied_deferred_fact_never_returns_old_as_current(sql_scope, ingestion, monkeypatch, large):
    old, unrelated, _, job_id, instance, result, _ = prepare(sql_scope, ingestion, monkeypatch, large=large)
    with sql_scope.factory.begin() as session:
        session.get(ExtractionJob, job_id).status = ExtractionJobStatus.completed
    with sql_scope.factory() as session:
        job = session.get(ExtractionJob, job_id)
        assert job.runtime_applied_at is not None and job.runtime_visibility_finalized_at is not None
        assert job.preference_intent_admission["receipt"]["deferred_facts"] == 1
        assert job.runtime_residual_hold is not None and not session.get(Memory, old).is_archived
        before = copy.deepcopy(job.runtime_residual_hold)
    items, visibility, _ = read(sql_scope, [old, unrelated])
    assert str(old) not in {m.id for m in items} and visibility.state == "unavailable"
    assert visibility.scope == ("user_learned_context" if large else "affected_context")
    assert (str(unrelated) in {m.id for m in items}) == (not large)
    assert instance.process(job_id) == result
    with sql_scope.factory() as session:
        assert session.get(ExtractionJob, job_id).runtime_residual_hold == before


def test_accepted_preference_remains_available_beside_known_pending_fact(sql_scope, ingestion, monkeypatch):
    old, unrelated, pref, _, _, _, _ = prepare(sql_scope, ingestion, monkeypatch, mixed=True)
    items, visibility, _ = read(sql_scope, [old, unrelated, pref], context=preference_context())
    assert visibility.scope == "affected_context" and str(old) not in {m.id for m in items}
    assert any("Python" in m.content for m in items) and str(unrelated) in {m.id for m in items}


def test_low_authority_challenge_does_not_hide_stronger_fact(sql_scope, ingestion, monkeypatch):
    old, unrelated, _, job_id, _, _, _ = prepare(sql_scope, ingestion, monkeypatch, authority=True)
    with sql_scope.factory() as session:
        job = session.get(ExtractionJob, job_id)
        assert job.runtime_residual_hold is None
        assert job.preference_intent_admission["receipt"]["fact_outcomes"][0]["state"] == "authority_protected"
    items, visibility, _ = read(sql_scope, [old, unrelated])
    assert visibility is None and str(old) in {m.id for m in items}


def test_later_accepted_exact_correction_releases_completed_residual(sql_scope, ingestion, monkeypatch):
    old, unrelated, _, old_job, _, _, _ = prepare(sql_scope, ingestion, monkeypatch)
    monkeypatch.setattr(get_settings(), "runtime_target_release_enabled", True)
    new_id = uuid.UUID(ingestion.send(messages=[{"role": "user", "content": FACT["quote"]}])["job_id"])
    instance, _, _ = worker(sql_scope, [reply(preferences=[], nonpreferences=[FACT]), _decision(old, relation="supersedes")])
    result = instance.process(new_id)
    with sql_scope.factory() as session:
        assert session.get(ExtractionJob, old_job).runtime_hold_released_at is not None
        assert session.get(RuntimeJobReconciliation, old_job).record_json["outcome"] == "residual_fact_hold"
    ids = [uuid.UUID(m["id"]) for m in result["stored_memories"]]
    items, visibility, _ = read(sql_scope, [old, unrelated, *ids])
    assert visibility is None and str(old) not in {m.id for m in items} and any("October 18" in m.content for m in items)


def test_unknown_deferred_context_survives_retention(sql_scope, ingestion, monkeypatch):
    from datetime import UTC, datetime, timedelta
    old, unrelated, _, job_id, _, _, _ = prepare(sql_scope, ingestion, monkeypatch, large=True)
    with sql_scope.factory.begin() as session:
        session.get(ExtractionJob, job_id).raw_payload_expires_at = datetime.now(UTC) - timedelta(seconds=1)
    monkeypatch.setattr(provenance_tasks, "build_sync_session_factory", lambda: sql_scope.factory)
    provenance_tasks.redact_expired_extraction_payloads.run()
    assert not read(sql_scope, [old, unrelated])[0]
    with sql_scope.factory() as session:
        assert session.get(ExtractionJob, job_id).runtime_residual_hold is not None


def test_legacy_receipt_is_blocked_until_bounded_metadata_projection(sql_scope, ingestion, monkeypatch):
    old, _, _, job_id, _, _, _ = prepare(sql_scope, ingestion, monkeypatch)
    with sql_scope.factory.begin() as session:
        job = session.get(ExtractionJob, job_id)
        private = copy.deepcopy(job.preference_intent_admission)
        private["receipt"].pop("fact_outcomes")
        job.preference_intent_admission = private
        job.runtime_visibility_finalized_at = None
        job.runtime_residual_hold = None
        session.delete(session.get(RuntimeJobReconciliation, job_id))
    assert read(sql_scope, [old])[1].scope == "user_learned_context"
    before = backfill_visibility_batch(sql_scope.factory, tenant_id=sql_scope.tenant_id)
    assert before["eligible"] == 1 and before["projected"] == 0
    report = backfill_visibility_batch(sql_scope.factory, tenant_id=sql_scope.tenant_id, apply=True)
    assert report["projected"] == 1 and report["unrecoverable"] == 0
    assert read(sql_scope, [old])[1].scope == "user_learned_context"
    assert backfill_visibility_batch(sql_scope.factory, tenant_id=sql_scope.tenant_id, apply=True)["projected"] == 0


def test_visibility_audit_failure_rolls_back_memory_receipt_and_hold(sql_scope, ingestion, monkeypatch):
    old = fact(sql_scope)
    enable(sql_scope, monkeypatch)
    job_id = uuid.UUID(ingestion.send(messages=[{"role": "user", "content": FACT["quote"]}])["job_id"])
    instance, _, _ = worker(sql_scope, [reply(preferences=[], nonpreferences=[FACT]), _decision(old, state="unclear", relation="ambiguous")])
    def reject(_mapper, _connection, row):
        if (row.metadata_json or {}).get("event") == "runtime_fact_visibility_withheld":
            raise RuntimeError("controlled residual audit failure")
    event.listen(AuditLog, "before_insert", reject)
    try:
        with pytest.raises(RuntimeError, match="residual audit failure"):
            instance.process(job_id)
    finally:
        event.remove(AuditLog, "before_insert", reject)
    with sql_scope.factory() as session:
        job = session.get(ExtractionJob, job_id)
        assert job.runtime_applied_at is None and job.runtime_visibility_finalized_at is None
        assert session.get(RuntimeJobReconciliation, job_id) is None


def test_missing_historical_receipt_is_not_silently_repaired(sql_scope, ingestion, monkeypatch):
    old, _, _, job_id, _, _, _ = prepare(sql_scope, ingestion, monkeypatch)
    with sql_scope.factory.begin() as session:
        job = session.get(ExtractionJob, job_id)
        job.runtime_visibility_finalized_at = None
        job.runtime_residual_hold = None
        job.preference_intent_admission = job.preference_intent_admission_envelope = None
        session.delete(session.get(RuntimeJobReconciliation, job_id))
    report = backfill_visibility_batch(sql_scope.factory, tenant_id=sql_scope.tenant_id, apply=True)
    assert report["unrecoverable"] == 1 and report["projected"] == 0
    assert read(sql_scope, [old])[1].scope == "user_learned_context"


def test_lower_authority_ledger_veto_does_not_hide_current_fact(sql_scope, ingestion, monkeypatch):
    old = fact(sql_scope)
    with sql_scope.factory.begin() as session:
        session.add(MemoryClaim(id=uuid.uuid4(), tenant_id=sql_scope.tenant_id, proxy_user_id=sql_scope.proxy_id,
            category="fact", claim_fingerprint="f" * 64, subject_key="exam", predicate_key="date",
            active_memory_id=old, active_value="October 10", authority_priority=90, status="active"))
    enable(sql_scope, monkeypatch)
    job_id = uuid.UUID(ingestion.send(messages=[{"role": "user", "content": FACT["quote"]}])["job_id"])
    instance, _, _ = worker(sql_scope, [reply(preferences=[], nonpreferences=[FACT]), _decision(old, relation="supersedes")])
    instance.process(job_id)
    items, visibility, _ = read(sql_scope, [old])
    assert visibility is None and len(items) == 1
    with sql_scope.factory() as session:
        assert session.get(ExtractionJob, job_id).preference_intent_admission["receipt"]["fact_outcomes"][0]["state"] == "authority_protected"


def test_corrupt_residual_binding_and_flag_rollback_keep_guard(sql_scope, ingestion, monkeypatch):
    old, unrelated, _, job_id, _, _, _ = prepare(sql_scope, ingestion, monkeypatch)
    with sql_scope.factory.begin() as session:
        row = session.get(ExtractionJob, job_id)
        original = copy.deepcopy(row.runtime_residual_hold)
        row.runtime_residual_hold = {**original, "projection_sha256": "0" * 64}
    assert read(sql_scope, [old, unrelated])[1].scope == "user_learned_context"
    with sql_scope.factory.begin() as session:
        session.get(ExtractionJob, job_id).runtime_residual_hold = original
    monkeypatch.setattr(get_settings(), "runtime_preference_enabled", False)
    assert read(sql_scope, [old, unrelated])[1].scope == "affected_context"
    monkeypatch.setattr(get_settings(), "runtime_target_withholding_enabled", False)
    assert read(sql_scope, [old, unrelated])[1].scope == "user_learned_context"


def test_missing_residual_audit_binding_cannot_narrow_reads(sql_scope, ingestion, monkeypatch):
    old, unrelated, _, job_id, _, _, _ = prepare(sql_scope, ingestion, monkeypatch)
    with sql_scope.factory.begin() as session:
        session.delete(session.get(RuntimeJobReconciliation, job_id))
    items, visibility, _ = read(sql_scope, [old, unrelated])
    assert not items and visibility.scope == "user_learned_context"


@pytest.mark.parametrize("corruption", ["index", "state", "version", "other_captured_target"])
def test_per_item_receipt_requires_exact_indices_counts_and_targets(sql_scope, ingestion, monkeypatch, corruption):
    from pydantic import ValidationError

    from api.services.runtime_preference_worker import RuntimePreferenceRecord

    _, _, _, job_id, _, _, _ = prepare(sql_scope, ingestion, monkeypatch)
    with sql_scope.factory() as session:
        raw = copy.deepcopy(session.get(ExtractionJob, job_id).preference_intent_admission)
    item = raw["receipt"]["fact_outcomes"][0]
    if corruption == "index":
        item["index"] = 1
    elif corruption == "state":
        item.update(state="rejected", target=None)
    elif corruption == "version":
        item["target"]["version"] = "b" * 64
    else:
        item["target"] = next(t for t in raw["fact_context"] if t["memory_id"] != item["target"]["memory_id"])
    with pytest.raises(ValidationError, match="fact outcomes"):
        RuntimePreferenceRecord.model_validate_json(json.dumps(raw))


def test_legacy_resolved_receipt_can_be_projected_without_provider_or_memory_writes(sql_scope, ingestion, monkeypatch):
    enable(sql_scope, monkeypatch)
    old = fact(sql_scope)
    job_id = uuid.UUID(ingestion.send(messages=[{"role": "user", "content": "hello"}])["job_id"])
    instance, _, _ = worker(sql_scope, [reply(preferences=[])])
    instance.process(job_id)
    with sql_scope.factory.begin() as session:
        row = session.get(ExtractionJob, job_id)
        raw = copy.deepcopy(row.preference_intent_admission)
        raw["receipt"].pop("fact_outcomes")
        row.preference_intent_admission = raw
        row.runtime_visibility_finalized_at = None
    assert read(sql_scope, [old])[1].scope == "user_learned_context"
    report = backfill_visibility_batch(sql_scope.factory, tenant_id=sql_scope.tenant_id, apply=True)
    assert report["projected"] == 1 and report["unrecoverable"] == 0
    items, visibility, _ = read(sql_scope, [old])
    assert visibility is None and len(items) == 1
