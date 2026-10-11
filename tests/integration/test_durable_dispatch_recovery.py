"""Approved focused recovery regressions; isolated real SQL/Redis only."""
import importlib.util
import json
import subprocess
import sys
import time
import uuid
from urllib.parse import quote
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier, Event
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import delete, select

from api.db.models import ApiKey, ExtractionJob, ExtractionJobStatus, ProxyUser, Tenant
from api.tasks import watchdog_tasks

spec = importlib.util.spec_from_file_location("dispatch_regression_helpers",
    Path(__file__).with_name("test_durable_add_replay.py"))
helpers = importlib.util.module_from_spec(spec)
spec.loader.exec_module(helpers)
@pytest.fixture
def database():
    # Each recovery test scans all bound jobs; give it its own disposable schema
    # so earlier synthetic jobs cannot become recovery candidates.
    yield from helpers.database.__wrapped__()

scope = helpers.scope
RUNTIME = Path(__file__).with_name("dispatch_recovery_local_runtime.py")


def test_cache_failure_cannot_gate_dispatch(scope):
    sent = []
    with scope.factory() as session:
        svc = helpers.service(scope, session)
        async def send(job):
            sent.append((job["job_id"], job["celery_task_id"]))
            return SimpleNamespace(id=job["celery_task_id"])
        svc.dispatch_task = True
        svc._send_extraction_task = send
        svc.cache_service.set_job_status = AsyncMock(side_effect=RuntimeError("cache unavailable"))
        result = helpers.run(svc.queue_memory_add(**helpers.args(scope)))
    assert result["status"] == "queued" and sent == [(result["job_id"], result["job_id"])]
    with scope.factory() as session:
        assert session.get(ExtractionJob, uuid.UUID(result["job_id"])).celery_task_id == result["job_id"]
    assert helpers.admit(scope)["job_id"] == result["job_id"]


def test_publish_failure_remains_same_queued_receipt_and_reservation(scope):
    with scope.factory() as session:
        svc = helpers.service(scope, session)
        svc.dispatch_task = True
        svc._send_extraction_task = AsyncMock(side_effect=RuntimeError("broker unavailable"))
        result = helpers.run(svc.queue_memory_add(**helpers.args(scope)))
    assert result["status"] == "queued"
    assert helpers.admit(scope)["job_id"] == result["job_id"]
    assert scope.client.get(helpers._tenant_queue_depth_key(str(scope.tenant_id), result["queue_name"])) == "1"
    calls = []
    recovered = watchdog_tasks.run_durable_dispatch_recovery(session_factory=scope.factory,
        publisher=lambda payload, task_id, queue: calls.append((payload, task_id, queue)))
    assert recovered["published"] == 1 and calls[0][1] == result["job_id"]
    assert helpers.counts(scope)["extraction_jobs"] == 1


def test_lost_ack_republishes_same_id_without_new_admission(scope):
    original = helpers.admit(scope)
    before = helpers.counts(scope)
    sent = []
    def lost_ack(payload, task_id, queue):
        sent.append(task_id)
        raise ConnectionError("synthetic acknowledgement lost after publication")
    first = watchdog_tasks.run_durable_dispatch_recovery(session_factory=scope.factory, publisher=lost_ack)
    second = watchdog_tasks.run_durable_dispatch_recovery(session_factory=scope.factory,
        publisher=lambda payload, task_id, queue: sent.append(task_id))
    assert first["errors"] == 1 and second["published"] == 1
    assert sent == [original["job_id"], original["job_id"]] and helpers.counts(scope) == before


def test_five_way_recovery_and_initial_publication_share_id(scope):
    barrier = Barrier(4)
    started, release = Event(), Event()
    sent = []
    def initial():
        with scope.factory() as session:
            svc = helpers.service(scope, session)
            svc.dispatch_task = True
            async def paused_send(job):
                sent.append(job["celery_task_id"])
                started.set()
                assert release.wait(timeout=10)
                return SimpleNamespace(id=job["celery_task_id"])
            svc._send_extraction_task = paused_send
            return helpers.run(svc.queue_memory_add(**helpers.args(scope)))
    def attempt(_):
        barrier.wait(timeout=10)
        return watchdog_tasks.run_durable_dispatch_recovery(session_factory=scope.factory,
            publisher=lambda payload, task_id, queue: sent.append(task_id))
    with ThreadPoolExecutor(max_workers=5) as pool:
        admission = pool.submit(initial)
        assert started.wait(timeout=10)
        try:
            list(pool.map(attempt, range(4)))
        finally:
            release.set()
        original = admission.result(timeout=10)
    assert len(sent) >= 2 and set(sent) == {original["job_id"]}
    assert helpers.counts(scope)["extraction_jobs"] == 1
    assert scope.client.get(helpers._tenant_queue_depth_key(str(scope.tenant_id), original["queue_name"])) == "1"


def test_legacy_bound_receipt_assigns_delivery_id_before_publication(scope):
    original = helpers.admit(scope)
    with scope.factory.begin() as session:
        session.get(ExtractionJob, uuid.UUID(original["job_id"])).celery_task_id = None
    sent = []
    publish = lambda payload, task_id, queue: sent.append(task_id)
    first = watchdog_tasks.run_durable_dispatch_recovery(session_factory=scope.factory, publisher=publish)
    assert first["published"] == 0 and sent == []
    second = watchdog_tasks.run_durable_dispatch_recovery(session_factory=scope.factory, publisher=publish)
    assert second["published"] == 1 and sent == [original["job_id"]]


def test_default_recovery_factory_preserves_schema_options_with_finite_timeouts(scope, monkeypatch):
    original = helpers.admit(scope)
    monkeypatch.setattr(watchdog_tasks, "get_database_url", lambda:
        helpers.PG_URL + "?options=" + quote("-c search_path=" + scope.schema))
    sent = []
    outcome = watchdog_tasks.run_durable_dispatch_recovery(
        publisher=lambda payload, task_id, queue: sent.append(task_id))
    assert outcome["published"] == 1 and sent == [original["job_id"]]


@pytest.mark.parametrize("state", [ExtractionJobStatus.processing, ExtractionJobStatus.completed, ExtractionJobStatus.dead])
def test_recovery_never_takes_over_started_or_terminal_jobs(scope, state):
    original = helpers.admit(scope)
    with scope.factory.begin() as session:
        row = session.get(ExtractionJob, uuid.UUID(original["job_id"]))
        row.status = state
    sent = []
    result = watchdog_tasks.run_durable_dispatch_recovery(session_factory=scope.factory,
        publisher=lambda *args: sent.append(args))
    assert result["checked"] == 0 and sent == []


@pytest.mark.parametrize("action", ["receipt", "owner", "tenant", "key_revoked", "key_read_only", "owner_blocked", "tenant_inactive"])
def test_deletion_and_revocation_prevent_recovery_publication(scope, action):
    original = helpers.admit(scope)
    with scope.factory.begin() as session:
        if action == "receipt": session.execute(delete(ExtractionJob))
        elif action == "owner": session.execute(delete(ProxyUser).where(ProxyUser.id == scope.proxy_id))
        elif action == "tenant": session.execute(delete(Tenant).where(Tenant.id == scope.tenant_id))
        elif action == "key_revoked": session.get(ApiKey, scope.key_id).is_active = False
        elif action == "key_read_only": session.get(ApiKey, scope.key_id).permissions = ["read"]
        elif action == "owner_blocked": session.get(ProxyUser, scope.proxy_id).is_blocked = True
        else: session.get(Tenant, scope.tenant_id).is_active = False
    sent = []
    watchdog_tasks.run_durable_dispatch_recovery(session_factory=scope.factory,
        publisher=lambda *args: sent.append(args))
    assert sent == []


def child(scope, mode, *extra):
    completed = subprocess.run([sys.executable, "-B", str(RUNTIME), mode, scope.schema,
        str(scope.tenant_id), str(scope.proxy_id), str(scope.key_id), scope.external, *extra],
        capture_output=True, text=True, timeout=20)
    return completed


def test_actual_api_process_termination_after_sql_commit(scope):
    result = child(scope, "api-kill")
    assert result.returncode == 73, result.stderr
    assert helpers.counts(scope)["extraction_jobs"] == 1
    with scope.factory() as session:
        row = session.scalars(select(ExtractionJob)).one()
        job_id = str(row.id)
        assert row.status == ExtractionJobStatus.queued and row.celery_task_id == job_id
    sent = []
    outcome = watchdog_tasks.run_durable_dispatch_recovery(session_factory=scope.factory,
        publisher=lambda payload, task_id, queue: sent.append(task_id))
    assert outcome["published"] == 1 and sent == [job_id]


@pytest.mark.parametrize("mode", ["publisher-kill-before", "publisher-kill-after"])
def test_actual_publisher_termination_retains_recoverable_same_receipt(scope, mode):
    original = helpers.admit(scope)
    result = child(scope, mode)
    assert result.returncode == 74, result.stderr
    sent = []
    outcome = watchdog_tasks.run_durable_dispatch_recovery(session_factory=scope.factory,
        publisher=lambda payload, task_id, queue: sent.append(task_id))
    assert outcome["published"] == 1 and sent == [original["job_id"]]
    assert helpers.counts(scope)["extraction_jobs"] == 1
    scope.client.delete(original["queue_name"])


def test_real_scheduled_beat_broker_worker_recovery(scope, tmp_path):
    report = Path(__file__).resolve().parents[2] / "benchmarks/internal/reports/dispatch-recovery-repair-20261011-scheduled-v2.json"
    if report.exists(): pytest.fail("Timing artifact exists; rerun needs explicit evidence decision")
    logs, processes = [], []
    try:
        for mode in ("worker", "beat"):
            logfile = (tmp_path / (mode + ".log")).open("w", encoding="utf-8")
            logs.append(logfile)
            processes.append(subprocess.Popen([sys.executable, "-B", str(RUNTIME), mode, scope.schema,
                str(scope.tenant_id), str(scope.proxy_id), str(scope.key_id), scope.external],
                stdout=logfile, stderr=subprocess.STDOUT))
        # Readiness through disposable Redis, not a fixed startup assumption.
        readiness_deadline = time.monotonic() + 20
        while time.monotonic() < readiness_deadline:
            if scope.client.get("dispatch-test-worker-ready") and scope.client.get("dispatch-test-beat-ready"):
                break
            assert all(p.poll() is None for p in processes), [p.poll() for p in processes]
            time.sleep(.1)
        else: pytest.fail("Isolated Beat/worker failed readiness; inspect test-local logs")
        killed = child(scope, "api-kill")
        assert killed.returncode == 73, killed.stderr
        removed_at = time.monotonic()
        with scope.factory() as session:
            row = session.scalars(select(ExtractionJob)).one()
            job_id, queue = str(row.id), row.queue_name
        while time.monotonic() - removed_at <= 30:
            assert all(p.poll() is None for p in processes), "Beat/worker exited; timing experiment invalid"
            with scope.factory() as session:
                row = session.get(ExtractionJob, uuid.UUID(job_id))
                if row.status in {ExtractionJobStatus.completed, ExtractionJobStatus.dead, ExtractionJobStatus.failed}:
                    break
            time.sleep(.05)
        elapsed = time.monotonic() - removed_at
        observed = helpers.counts(scope)
        passed = row.status == ExtractionJobStatus.completed and observed["memories"] == 1 and elapsed <= 30
        evidence = {"base_commit": "4bf8884737ef58416bb34b9d831b0836333d21c0",
            "candidate": "approved working-tree dispatch repair; repin required", "elapsed_s": round(elapsed, 3),
            "job_id": job_id, "status": row.status.value, "counts": observed,
            "scheduled_recovery_result": "PASS" if passed else "FAIL",
            "workload": "one synthetic explicit preference; isolated real Beat/Redis/solo worker",
            "limitations": ["controlled embeddings and declaration pipeline; no provider calls",
                "Windows solo worker; prefork time-limit enforcement not established",
                "not independent pilot acceptance or production; worker-started crash excluded"]}
        report.write_text(json.dumps(evidence, indent=2) + "\n", encoding="utf-8")
        assert passed, evidence
        # Actual broker redelivery uses the same ID and existing worker claim.
        published = child(scope, "duplicate", job_id, queue)
        assert published.returncode == 0, published.stderr
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and not scope.client.get("dispatch-test-duplicate-ignored"):
            time.sleep(.05)
        assert scope.client.get("dispatch-test-duplicate-ignored")
        assert helpers.counts(scope) == observed
        evidence["duplicate_delivery_counts_unchanged"] = True
        report.write_text(json.dumps(evidence, indent=2) + "\n", encoding="utf-8")
    finally:
        for process in processes:
            process.terminate()
        for process in processes:
            try: process.wait(timeout=5)
            except subprocess.TimeoutExpired: process.kill(); process.wait(timeout=5)
        for logfile in logs: logfile.close()
        scope.client.delete("dispatch-test-worker-ready", "dispatch-test-beat-ready", "dispatch-test-duplicate-ignored")
