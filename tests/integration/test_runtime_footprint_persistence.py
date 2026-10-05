"""Frozen footprint persistence; read guards stay conservative in this stage."""
import copy
import json
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import event
from sqlalchemy.exc import IntegrityError

from api.db.models import ExtractionJob
from api.services.preference_intent_admission_service import (
    PreferenceAdmissionUnavailable,
    _hash,
)
from api.services.runtime_preference_worker import RuntimePreferenceTransactionService
from api.tasks import provenance_tasks
from tests.integration.test_preference_choice_operations import counts
from tests.integration.test_preference_intent_admission import args, job
from tests.integration.test_runtime_pending_visibility import read
from tests.integration.test_runtime_preference_worker import enable, seed, worker
from tests.integration.test_runtime_preference_worker import (
    forbid_real_providers as _forbid,
)
from tests.integration.test_runtime_preference_worker import sql_scope as _sql_scope
from tests.unit.test_preference_intent_contract import intent, reply

sql_scope = _sql_scope
forbid_real_providers = _forbid


def prepare(scope, monkeypatch, **payload):
    old = seed(scope, monkeypatch)
    enable(scope, monkeypatch)
    job_id = job(scope, messages=[{"role": "user", "content": intent()["quote"]}], **payload)
    instance, model, embeddings = worker(scope, [reply()])
    captured = instance.transactions.capture(**args(scope, job_id))
    return old, job_id, instance, model, embeddings, captured


def footprint(scope, job_id):
    with scope.factory() as session:
        return copy.deepcopy(session.get(ExtractionJob, job_id).runtime_withholding_footprint)


def test_capture_has_no_bound_and_admission_commits_footprint_with_fixed_record(sql_scope, monkeypatch):
    old, job_id, instance, model, _, captured = prepare(sql_scope, monkeypatch)
    assert footprint(sql_scope, job_id) is None
    record = instance.transactions.admit(reply(), llm_service=model, **args(sql_scope, job_id),
        capture_id=captured.capture_id)
    saved = footprint(sql_scope, job_id)
    assert saved["coverage"] == "targets" and saved["decision_sha256"] == record.decision_sha256
    assert saved["capture_id"] == str(captured.capture_id) and saved["job_id"] == str(job_id)
    assert not model.calls
    # A footprint does not release current context, even when its target is known.
    assert read(sql_scope, [old])[1].state == "processing"
    result = instance.process(job_id)
    assert result["memories_created"] == 1 and footprint(sql_scope, job_id) == saved
    assert "runtime_withholding_footprint" not in json.dumps(result)
    restarted, no_model, no_embeddings = worker(sql_scope)
    assert restarted.process(job_id) == result and footprint(sql_scope, job_id) == saved
    assert not no_model.calls and not no_embeddings.calls


def test_admission_commit_failure_does_not_leave_a_target_projection(sql_scope, monkeypatch):
    _, job_id, instance, model, _, captured = prepare(sql_scope, monkeypatch)
    def reject_write(_mapper, _connection, row):
        if row.id == job_id and row.runtime_withholding_footprint is not None:
            raise RuntimeError("controlled footprint commit failure")
    event.listen(ExtractionJob, "before_update", reject_write)
    try:
        with pytest.raises(RuntimeError, match="controlled footprint commit failure"):
            instance.transactions.admit(reply(), llm_service=model, **args(sql_scope, job_id),
                capture_id=captured.capture_id)
    finally:
        event.remove(ExtractionJob, "before_update", reject_write)
    assert footprint(sql_scope, job_id) is None
    with sql_scope.factory() as session:
        assert RuntimePreferenceTransactionService._read(session.get(ExtractionJob, job_id)).state == "captured"


def test_retention_keeps_minimal_footprint_but_cannot_project_application(sql_scope, monkeypatch):
    old, job_id, instance, model, _, captured = prepare(sql_scope, monkeypatch)
    instance.transactions.admit(reply(), llm_service=model, **args(sql_scope, job_id), capture_id=captured.capture_id)
    before = footprint(sql_scope, job_id)
    with sql_scope.factory.begin() as session:
        session.get(ExtractionJob, job_id).raw_payload_expires_at = datetime.now(UTC) - timedelta(seconds=1)
    monkeypatch.setattr(provenance_tasks, "build_sync_session_factory", lambda: sql_scope.factory)
    assert provenance_tasks.redact_expired_extraction_payloads.run()["redacted_jobs"] == 1
    with sql_scope.factory() as session:
        row = session.get(ExtractionJob, job_id)
        assert row.runtime_withholding_footprint == before and row.runtime_applied_at is None
        assert row.preference_intent_admission is None and "messages" not in row.payload
    assert read(sql_scope, [old])[1].state == "unavailable"


def test_recomputed_corrupt_projection_cannot_rebind_and_runs_no_provider(sql_scope, monkeypatch):
    old, job_id, instance, model, embeddings, captured = prepare(sql_scope, monkeypatch)
    instance.transactions.admit(reply(), llm_service=model, **args(sql_scope, job_id), capture_id=captured.capture_id)
    with sql_scope.factory.begin() as session:
        row = session.get(ExtractionJob, job_id)
        changed = {**row.runtime_withholding_footprint, "coverage": "user_learned_context",
                   "reason": "unknown_preference", "preference_fingerprints": []}
        changed["projection_sha256"] = _hash({k: v for k, v in changed.items() if k != "projection_sha256"})
        row.runtime_withholding_footprint = changed
    before = counts(sql_scope)
    with pytest.raises(PreferenceAdmissionUnavailable, match="intent_footprint_changed"):
        instance.process(job_id)
    assert counts(sql_scope) == before and not model.calls and not embeddings.calls
    assert read(sql_scope, [old])[1].state == "processing"


def test_public_payload_cannot_supply_a_footprint(sql_scope, monkeypatch):
    _, job_id, instance, model, _, captured = prepare(sql_scope, monkeypatch,
        runtime_withholding_footprint={"coverage": "targets", "authority": 100})
    assert footprint(sql_scope, job_id) is None
    instance.transactions.admit(reply(), llm_service=model, **args(sql_scope, job_id), capture_id=captured.capture_id)
    assert "authority" not in footprint(sql_scope, job_id)


@pytest.mark.parametrize("invalid", [[], "untrusted", {"oversized": "x" * 8192}])
def test_sql_enforces_projection_object_and_size_bound(sql_scope, monkeypatch, invalid):
    _, job_id, _, _, _, _ = prepare(sql_scope, monkeypatch)
    with pytest.raises(IntegrityError), sql_scope.factory.begin() as session:
        session.get(ExtractionJob, job_id).runtime_withholding_footprint = invalid
    assert footprint(sql_scope, job_id) is None
