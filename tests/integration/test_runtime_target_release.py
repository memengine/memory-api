"""Actual SQL writes discharge only matching quarantined targets; no paid models."""
import copy
import uuid

import pytest
from sqlalchemy import event, select

from api.db.models import (
    AuditLog,
    ExtractionJob,
    Memory,
    MemoryClaim,
    RuntimeJobReconciliation,
)
from api.schemas.runtime_target_release import read_target_releases
from api.settings import get_settings
from tests.integration.test_preference_fact_transaction import fact
from tests.integration.test_preference_intent_admission import args
from tests.integration.test_runtime_pending_visibility import preference_context, read
from tests.integration.test_runtime_preference_worker import worker
from tests.integration.test_runtime_quarantine import expire, reconcile
from tests.integration.test_runtime_recovery import forbid_real_providers as _forbid
from tests.integration.test_runtime_recovery import ingestion as _ingestion
from tests.integration.test_runtime_recovery import prepare, saved
from tests.integration.test_runtime_recovery import recovery_api as _api
from tests.integration.test_runtime_recovery import sql_scope as _scope
from tests.unit.test_preference_intent_contract import intent, reply

sql_scope, ingestion, recovery_api, forbid_real_providers = _scope, _ingestion, _api, _forbid


def held(scope, ingestion, monkeypatch, api, *, state="validated"):
    old, job_id, _, _, _ = prepare(scope, ingestion, monkeypatch, state)
    expire(scope, job_id)
    assert reconcile(api, job_id).status_code == 200
    monkeypatch.setattr(get_settings(), "runtime_target_withholding_enabled", True)
    monkeypatch.setattr(get_settings(), "runtime_target_release_enabled", True)
    return old, job_id


def apply(scope, ingestion, raw=None, text=None, relations=()):
    raw = raw or reply()
    text = text or raw["preferences"][0]["quote"]
    new_id = uuid.UUID(ingestion.send(messages=[{"role": "user", "content": text}])["job_id"])
    instance, model, embedder = worker(scope, [raw, *relations])
    result = instance.process(new_id)
    return new_id, instance, result, model, embedder


def release_record(scope, job_id):
    from api.services.runtime_preference_footprint import RuntimePreferenceFootprint
    with scope.factory() as session:
        job = session.get(ExtractionJob, job_id)
        row = session.get(RuntimeJobReconciliation, job_id)
        return read_target_releases(row.release_json, RuntimePreferenceFootprint.model_validate(job.runtime_withholding_footprint))


def release_audits(scope):
    with scope.factory() as session:
        return list(session.scalars(select(AuditLog).where(AuditLog.proxy_user_id == scope.proxy_id,
            AuditLog.metadata_json["event"].astext == "runtime_targets_released")))


def test_accepted_replacement_releases_exact_identity_without_applying_failed_job(sql_scope, ingestion, monkeypatch, recovery_api):
    old, old_job = held(sql_scope, ingestion, monkeypatch, recovery_api)
    before = saved(sql_scope, old_job)
    source_job, instance, result, model, embedder = apply(sql_scope, ingestion)
    releases = release_record(sql_scope, old_job)
    assert len(releases) == 1 and releases[0].source_job_id == source_job and releases[0].outcome == "activated"
    assert str(releases[0].result_memory_id) in {m["id"] for m in result["stored_memories"]}
    assert saved(sql_scope, old_job) == before  # Dead job's raw record/receipt/footprint unchanged.
    with sql_scope.factory() as session:
        assert session.get(ExtractionJob, old_job).runtime_hold_released_at is not None
        assert session.get(ExtractionJob, old_job).runtime_applied_at is None
    items, visibility, _ = read(sql_scope, [old], context=preference_context())
    assert visibility is None and len(items) == 1 and "Python" in items[0].content
    assert len(release_audits(sql_scope)) == 1
    calls = len(model.calls), len(embedder.calls)
    assert instance.process(source_job) == result
    assert (len(model.calls), len(embedder.calls)) == calls and len(release_audits(sql_scope)) == 1
    assert recovery_api.call(old_job).status_code == 409


def test_reaffirm_current_value_requires_bound_runtime_hold(sql_scope, ingestion, monkeypatch, recovery_api):
    old, old_job = held(sql_scope, ingestion, monkeypatch, recovery_api)
    raw = reply([intent(action="reaffirm", value=None, quote="Keep my current C++ default")])
    _, _, _, _, embedder = apply(sql_scope, ingestion, raw)
    releases = release_record(sql_scope, old_job)
    assert len(releases) == 1 and releases[0].outcome == "reaffirmed" and releases[0].result_memory_id == old
    items, visibility, _ = read(sql_scope, [old], context=preference_context())
    assert visibility is None and len(items) == 1 and "C++" in items[0].content and not embedder.calls


@pytest.mark.parametrize("action", ["withhold", "tentative", "contextual", "abstain"])
def test_uncertain_or_unrepresented_input_cannot_release(sql_scope, ingestion, monkeypatch, recovery_api, action):
    old, old_job = held(sql_scope, ingestion, monkeypatch, recovery_api)
    raw = reply([intent(action=action, value=None if action in {"withhold", "abstain"} else "Python")])
    apply(sql_scope, ingestion, raw)
    assert not release_record(sql_scope, old_job) and not release_audits(sql_scope)
    assert read(sql_scope, [old], context=preference_context())[1] is not None


@pytest.mark.parametrize("change", ["release_disabled", "narrowing_disabled", "unknown", "authority", "scope"])
def test_gate_unknown_authority_and_scope_do_not_release(sql_scope, ingestion, monkeypatch, recovery_api, change):
    old, old_job = held(sql_scope, ingestion, monkeypatch, recovery_api,
        state="uncaptured" if change == "unknown" else "validated")
    if change.endswith("disabled"):
        monkeypatch.setattr(get_settings(), "runtime_target_release_enabled" if change == "release_disabled"
            else "runtime_target_withholding_enabled", False)
    if change == "authority":
        with sql_scope.factory.begin() as session:
            row = session.get(Memory, old)
            row.metadata_json = {**row.metadata_json, "provenance": {**row.metadata_json["provenance"],
                "authority_priority": 90, "authority_rules": {"default_priority": 90}}}
            claim = session.scalar(select(MemoryClaim).where(MemoryClaim.active_memory_id == old))
            claim.authority_priority = 90
    apply(sql_scope, ingestion, reply([intent(scope_ref="cedar")]) if change == "scope" else reply())
    with sql_scope.factory() as session:
        assert session.get(RuntimeJobReconciliation, old_job).release_json is None
        assert session.get(ExtractionJob, old_job).runtime_hold_released_at is None
    assert not release_audits(sql_scope)


def test_release_audit_failure_rolls_back_source_writes_and_discharge(sql_scope, ingestion, monkeypatch, recovery_api):
    old, old_job = held(sql_scope, ingestion, monkeypatch, recovery_api)
    new_id = uuid.UUID(ingestion.send(messages=[{"role": "user", "content": intent()["quote"]}])["job_id"])
    instance, _, _ = worker(sql_scope, [reply()])
    def reject(_mapper, _connection, row):
        if (row.metadata_json or {}).get("event") == "runtime_targets_released":
            raise RuntimeError("controlled release audit failure")
    event.listen(AuditLog, "before_insert", reject)
    try:
        with pytest.raises(RuntimeError, match="release audit failure"):
            instance.process(new_id)
    finally:
        event.remove(AuditLog, "before_insert", reject)
    with sql_scope.factory() as session:
        assert not session.get(Memory, old).is_archived
        assert session.get(ExtractionJob, new_id).runtime_applied_at is None
        assert session.get(ExtractionJob, old_job).runtime_hold_released_at is None
    assert not release_record(sql_scope, old_job) and not release_audits(sql_scope)


def mixed_hold(scope, ingestion, monkeypatch, api):
    from tests.integration.test_preference_intent_transaction import FACT, TEXT
    from tests.integration.test_runtime_preference_worker import enable, seed
    from tests.integration.test_runtime_recovery import mark_dead
    from tests.integration.test_source_decision_postgres import _decision
    old = seed(scope, monkeypatch)
    old_fact = fact(scope)
    enable(scope, monkeypatch)
    old_job = uuid.UUID(ingestion.send(messages=[{"role": "user", "content": TEXT}])["job_id"])
    instance, classifier, _ = worker(scope, [_decision(old_fact, relation="supersedes")])
    captured = instance.transactions.capture(**args(scope, old_job))
    instance.transactions.admit(reply(nonpreferences=[FACT]), llm_service=classifier,
        **args(scope, old_job), capture_id=captured.capture_id)
    mark_dead(scope, old_job)
    expire(scope, old_job)
    assert reconcile(api, old_job).status_code == 200
    monkeypatch.setattr(get_settings(), "runtime_target_withholding_enabled", True)
    monkeypatch.setattr(get_settings(), "runtime_target_release_enabled", True)
    return old, old_fact, old_job


def test_partial_language_then_fact_replacement_discharge_only_covered_targets(sql_scope, ingestion, monkeypatch, recovery_api):
    from tests.integration.test_preference_intent_transaction import FACT
    from tests.integration.test_source_decision_postgres import _decision
    old, old_fact, old_job = mixed_hold(sql_scope, ingestion, monkeypatch, recovery_api)
    _, _, first, _, _ = apply(sql_scope, ingestion)
    first_ids = [uuid.UUID(m["id"]) for m in first["stored_memories"]]
    items, visibility, _ = read(sql_scope, [old, old_fact, *first_ids], context=preference_context())
    assert visibility.scope == "affected_context" and str(old_fact) not in {m.id for m in items}
    assert any("Python" in m.content for m in items)
    with sql_scope.factory() as session:
        assert session.get(ExtractionJob, old_job).runtime_hold_released_at is None
    _, _, second, _, _ = apply(sql_scope, ingestion, reply(preferences=[], nonpreferences=[FACT]),
        text=FACT["quote"], relations=[_decision(old_fact, relation="supersedes")])
    second_ids = [uuid.UUID(m["id"]) for m in second["stored_memories"]]
    releases = release_record(sql_scope, old_job)
    assert {r.kind for r in releases} == {"preference", "fact"} and len(release_audits(sql_scope)) == 2
    items, visibility, _ = read(sql_scope, [old, old_fact, *first_ids, *second_ids], context=preference_context())
    assert visibility is None and str(old_fact) not in {m.id for m in items}
    assert any("October 18" in m.content for m in items)


@pytest.mark.parametrize("relation", ["novel", "compatible", "unknown"])
def test_fact_without_accepted_exact_supersession_cannot_release(sql_scope, ingestion, monkeypatch, recovery_api, relation):
    from tests.integration.test_preference_intent_transaction import FACT
    from tests.integration.test_source_decision_postgres import _decision
    _, old_fact, old_job = mixed_hold(sql_scope, ingestion, monkeypatch, recovery_api)
    apply(sql_scope, ingestion, reply(preferences=[], nonpreferences=[FACT]), text=FACT["quote"],
        relations=[_decision(old_fact, relation=relation)])
    assert not release_record(sql_scope, old_job) and not release_audits(sql_scope)


def test_source_accepted_before_quarantine_cannot_discharge_later_hold(sql_scope, ingestion, monkeypatch, recovery_api):
    _, old_job, _, _, _ = prepare(sql_scope, ingestion, monkeypatch, "validated")
    source = uuid.UUID(ingestion.send(messages=[{"role": "user", "content": intent()["quote"]}])["job_id"])
    expire(sql_scope, old_job)
    assert reconcile(recovery_api, old_job).status_code == 200
    monkeypatch.setattr(get_settings(), "runtime_target_release_enabled", True)
    monkeypatch.setattr(get_settings(), "runtime_target_withholding_enabled", True)
    instance, _, _ = worker(sql_scope, [reply()])
    instance.process(source)
    assert not release_record(sql_scope, old_job)


def test_partial_proof_corruption_and_flag_rollback_fail_closed(sql_scope, ingestion, monkeypatch, recovery_api):
    old, old_fact, old_job = mixed_hold(sql_scope, ingestion, monkeypatch, recovery_api)
    apply(sql_scope, ingestion)
    with sql_scope.factory.begin() as session:
        row = session.get(RuntimeJobReconciliation, old_job)
        original = copy.deepcopy(row.release_json)
        row.release_json = {**row.release_json, "projection_sha256": "0" * 64}
    assert read(sql_scope, [old, old_fact])[1].scope == "user_learned_context"
    with sql_scope.factory.begin() as session:
        session.get(RuntimeJobReconciliation, old_job).release_json = original
    monkeypatch.setattr(get_settings(), "runtime_target_release_enabled", False)
    items, visibility, _ = read(sql_scope, [old, old_fact], context=preference_context())
    assert not items and visibility is not None


def test_guard_lock_contention_aborts_without_provider_or_source_write(sql_scope, ingestion, monkeypatch, recovery_api):
    from sqlalchemy.exc import DBAPIError

    from tests.integration.test_source_decision_postgres import _embedding
    old, old_job = held(sql_scope, ingestion, monkeypatch, recovery_api)
    new_id = uuid.UUID(ingestion.send(messages=[{"role": "user", "content": intent()["quote"]}])["job_id"])
    instance, classifier, embedder = worker(sql_scope, [reply()])
    captured = instance.transactions.capture(**args(sql_scope, new_id))
    instance.transactions.admit(reply(), llm_service=classifier, **args(sql_scope, new_id), capture_id=captured.capture_id)
    with sql_scope.factory.begin() as session:
        session.get(ExtractionJob, old_job, with_for_update=True)
        with pytest.raises(DBAPIError) as error:
            instance.transactions.apply(**args(sql_scope, new_id), capture_id=captured.capture_id,
                embeddings={intent()["quote"]: _embedding(intent()["quote"])})
        assert (getattr(error.value.orig, "sqlstate", None) or getattr(error.value.orig, "pgcode", None)) == "55P03"
    assert not classifier.calls and not embedder.calls and not release_record(sql_scope, old_job)
    with sql_scope.factory() as session:
        assert not session.get(Memory, old).is_archived and session.get(ExtractionJob, new_id).runtime_applied_at is None


def test_retention_and_later_writes_do_not_resurrect_discharged_hold(sql_scope, ingestion, monkeypatch, recovery_api):
    from api.tasks import provenance_tasks
    old, old_job = held(sql_scope, ingestion, monkeypatch, recovery_api)
    source_job, _, _, _, _ = apply(sql_scope, ingestion)
    before = release_record(sql_scope, old_job)
    expire(sql_scope, source_job)
    monkeypatch.setattr(provenance_tasks, "build_sync_session_factory", lambda: sql_scope.factory)
    provenance_tasks.redact_expired_extraction_payloads.run()
    assert release_record(sql_scope, old_job) == before
    apply(sql_scope, ingestion, reply([intent(value="Rust", quote="Use Rust instead of Python")]))
    items, visibility, _ = read(sql_scope, [old], context=preference_context())
    assert visibility is None and len(items) == 1 and "Rust" in items[0].content
    monkeypatch.setattr(get_settings(), "runtime_target_release_enabled", False)
    assert read(sql_scope, [old], context=preference_context())[1] is not None


def test_unrelated_success_cannot_release_known_hold(sql_scope, ingestion, monkeypatch, recovery_api):
    from tests.integration.test_source_decision_postgres import _decision
    old, old_job = held(sql_scope, ingestion, monkeypatch, recovery_api)
    text = "my city is Pune"
    apply(sql_scope, ingestion, reply(preferences=[], nonpreferences=[
        {"category": "fact", "content": text, "quote": text, "turn_index": 0}]), text=text,
        relations=[_decision()])
    assert not release_record(sql_scope, old_job) and read(sql_scope, [old])[1] is not None


def test_backlog_write_page_progress_never_claims_unseen_jobs_resolved(sql_scope, ingestion, monkeypatch, recovery_api):
    from tests.integration.test_runtime_recovery import mark_dead
    old, first = held(sql_scope, ingestion, monkeypatch, recovery_api)
    jobs = [first]
    for _ in range(8):
        job_id = uuid.UUID(ingestion.send(messages=[{"role": "user", "content": intent()["quote"]}])["job_id"])
        instance, classifier, _ = worker(sql_scope)
        captured = instance.transactions.capture(**args(sql_scope, job_id))
        instance.transactions.admit(reply(), llm_service=classifier, **args(sql_scope, job_id), capture_id=captured.capture_id)
        mark_dead(sql_scope, job_id)
        expire(sql_scope, job_id)
        assert reconcile(recovery_api, job_id).status_code == 200
        jobs.append(job_id)
    source_job, instance, _, _, _ = apply(sql_scope, ingestion)
    with sql_scope.factory() as session:
        assert sum(session.get(ExtractionJob, j).runtime_hold_released_at is not None for j in jobs) == 8
    assert read(sql_scope, [old], context=preference_context())[1] is not None
    instance.process(source_job)  # An applied replay cannot acquire fresh release coverage.
    with sql_scope.factory() as session:
        assert sum(session.get(ExtractionJob, j).runtime_hold_released_at is not None for j in jobs) == 8
    apply(sql_scope, ingestion, reply([intent(action="reaffirm", value=None, quote="Keep the current Python default")]))
    assert read(sql_scope, [old], context=preference_context())[1] is None


def test_stale_source_capture_cannot_release(sql_scope, ingestion, monkeypatch, recovery_api):
    old, old_job = held(sql_scope, ingestion, monkeypatch, recovery_api)
    source = uuid.UUID(ingestion.send(messages=[{"role": "user", "content": intent()["quote"]}])["job_id"])
    instance, classifier, _ = worker(sql_scope)
    captured = instance.transactions.capture(**args(sql_scope, source))
    instance.transactions.admit(reply(), llm_service=classifier, **args(sql_scope, source), capture_id=captured.capture_id)
    with sql_scope.factory.begin() as session:
        session.get(Memory, old).content = "Changed after capture"
    instance.process(source)
    assert not release_record(sql_scope, old_job) and not release_audits(sql_scope)


def test_two_fixed_sources_do_not_double_release(sql_scope, ingestion, monkeypatch, recovery_api):
    from concurrent.futures import ThreadPoolExecutor

    from sqlalchemy.exc import DBAPIError

    from tests.integration.test_source_decision_postgres import _embedding
    old, old_job = held(sql_scope, ingestion, monkeypatch, recovery_api)
    instance, classifier, _ = worker(sql_scope)
    jobs = []
    for _ in range(2):
        source = uuid.UUID(ingestion.send(messages=[{"role": "user", "content": intent()["quote"]}])["job_id"])
        captured = instance.transactions.capture(**args(sql_scope, source))
        instance.transactions.admit(reply(), llm_service=classifier, **args(sql_scope, source), capture_id=captured.capture_id)
        jobs.append((source, captured.capture_id))
    def apply_fixed(item):
        source, capture = item
        return instance.transactions.apply(**args(sql_scope, source), capture_id=capture,
            embeddings={intent()["quote"]: _embedding(intent()["quote"])})
    failed = []
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [(item, pool.submit(apply_fixed, item)) for item in jobs]
        for item, future in futures:
            try:
                future.result()
            except DBAPIError as exc:
                assert (getattr(exc.orig, "sqlstate", None) or getattr(exc.orig, "pgcode", None)) in {"55P03", "40001"}
                failed.append(item)
    for item in failed:
        apply_fixed(item)  # Same frozen decision, no recapture or new classifier call.
    assert len(release_record(sql_scope, old_job)) == len(release_audits(sql_scope)) == 1 and not classifier.calls
    items, visibility, _ = read(sql_scope, [old], context=preference_context())
    assert visibility is None and len(items) == 1 and "Python" in items[0].content


def test_unresolved_guard_predicate_can_use_discharge_aware_index(sql_scope):
    from datetime import UTC, datetime

    from sqlalchemy import text
    with sql_scope.factory.begin() as session:
        # Synthetic indexed projections, not proof-generation or load metrics.
        session.add_all(ExtractionJob(id=uuid.uuid4(), tenant_id=sql_scope.tenant_id,
            proxy_user_id=sql_scope.proxy_id, external_user_id=sql_scope.external_id, runtime_route="preference_v1",
            runtime_hold_released_at=datetime.now(UTC), payload={}) for _ in range(2000))
        session.add(ExtractionJob(id=uuid.uuid4(), tenant_id=sql_scope.tenant_id,
            proxy_user_id=sql_scope.proxy_id, external_user_id=sql_scope.external_id, runtime_route="preference_v1", payload={}))
        session.flush()
        session.execute(text("ANALYZE extraction_jobs"))
        session.execute(text("SET LOCAL enable_seqscan = off"))
        plan = session.execute(text("EXPLAIN SELECT id FROM extraction_jobs WHERE tenant_id=:tenant AND proxy_user_id=:owner "
            "AND (runtime_route IS NOT NULL OR preference_intent_admission ? 'runtime_source_sha256') "
            "AND runtime_applied_at IS NULL AND runtime_hold_released_at IS NULL LIMIT 1"),
            {"tenant": sql_scope.tenant_id, "owner": sql_scope.proxy_id}).scalars().all()
        # Both managed partial indexes exclude fully discharged jobs. The new
        # visibility index also supports applied residuals; PostgreSQL may pick
        # either for this narrower historical quarantine predicate.
        assert any(name in " ".join(plan) for name in (
            "ix_extraction_jobs_runtime_unreleased", "ix_extraction_jobs_runtime_visibility")), plan
