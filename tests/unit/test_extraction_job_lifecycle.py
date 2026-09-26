from __future__ import annotations

from datetime import UTC
from datetime import datetime
from datetime import timedelta
from types import SimpleNamespace
from uuid import uuid4
from unittest.mock import AsyncMock
from unittest.mock import MagicMock

import pytest

from api.db.models import ExtractionJob
from api.db.models import ExtractionJobStatus
from api.db.models import QuotaMode
from api.services.memory_service import MemoryService
from api.errors import APIError
from api.services.quota_manager import QuotaEnvelope
from api.tasks import watchdog_tasks
from api.tasks import extraction_tasks


class FakeQuotaManager:
    def __init__(self) -> None:
        self.get_quota_envelope = AsyncMock(
            return_value=QuotaEnvelope(mode=QuotaMode.full, budget_remaining_pct=0.88, reset_at=None)
        )


class FakeDispatchTask:
    def __init__(self) -> None:
        self.calls = []

    def __call__(self, task_name, *args, **kwargs):
        self.calls.append((task_name, args, kwargs))
        return None


class FakeDispatchResultTask:
    def __call__(self, task_name, *args, **kwargs):
        return SimpleNamespace(id="celery-dispatch-1")


class FakeAsyncSession:
    def __init__(self) -> None:
        self.added = []
        self.commit = AsyncMock()
        self.flush = AsyncMock()
        self.execute = AsyncMock(return_value=SimpleNamespace(scalar_one_or_none=lambda: uuid4()))
        self.get = AsyncMock(return_value=None)

    def add(self, obj):
        self.added.append(obj)


class FakeDispatchAsyncSession(FakeAsyncSession):
    def __init__(self) -> None:
        super().__init__()
        self.execute = AsyncMock(side_effect=[
            SimpleNamespace(scalar_one_or_none=lambda: uuid4()),
            SimpleNamespace(rowcount=1),
        ])
        self.rollback = AsyncMock()


class FakeReservation:
    def __init__(self) -> None:
        self.queue_name = "starter-extraction"
        self.plan_tier = "starter"


@pytest.mark.asyncio
async def test_rejected_evidence_rolls_back_and_releases_reserved_queue_slot():
    session = FakeAsyncSession()
    session.rollback = AsyncMock()
    cache = MagicMock()
    service = MemoryService(
        session=session, cache_service=cache, qdrant_service=MagicMock(),
        quota_manager=FakeQuotaManager(), embedding_service=MagicMock(),
    )
    service.queue_router = SimpleNamespace(
        reserve_extraction_slot=AsyncMock(return_value=FakeReservation()),
        release_extraction_slot=AsyncMock(),
    )
    service._create_extraction_job = AsyncMock(
        side_effect=APIError(status_code=409, code="EVID_409", error="evidence_turn_payload_mismatch")
    )
    with pytest.raises(APIError):
        await service.queue_memory_add(
            requested_user_id=None, authenticated_user_id=None, agent_id=None,
            messages=[{"role": "user", "content": "I prefer concise answers."}],
            metadata={}, idempotency_key=None,
            tenant_id="11111111-1111-1111-1111-111111111111",
            external_user_id="test-user",
            proxy_user_id="22222222-2222-2222-2222-222222222222",
        )
    session.rollback.assert_awaited_once()
    service.queue_router.release_extraction_slot.assert_awaited_once()
    cache.set_job_status.assert_not_called()


@pytest.mark.asyncio
async def test_queue_memory_add_persists_extraction_job_row() -> None:
    session = FakeAsyncSession()
    cache_service = MagicMock()
    cache_service.get_idempotent_response = AsyncMock(return_value=None)
    cache_service.set_job_status = AsyncMock()
    cache_service.set_idempotent_response = AsyncMock()
    service = MemoryService(
        session=session,
        cache_service=cache_service,
        qdrant_service=MagicMock(),
        quota_manager=FakeQuotaManager(),
        proxy_user_service=SimpleNamespace(),
        dispatch_task=FakeDispatchTask(),
    )
    service.queue_router = SimpleNamespace(
        reserve_extraction_slot=AsyncMock(return_value=FakeReservation()),
        release_extraction_slot=AsyncMock(),
    )

    result = await service.queue_memory_add(
        requested_user_id=None,
        authenticated_user_id=None,
        agent_id=None,
        messages=[{"role": "user", "content": "hello"}],
        metadata={"session_id": "sess_1"},
        idempotency_key=None,
        tenant_id="11111111-1111-1111-1111-111111111111",
        external_user_id="external_user_123",
        proxy_user_id="22222222-2222-2222-2222-222222222222",
    )

    assert result["status"] == "queued"
    extraction_job = next(obj for obj in session.added if isinstance(obj, ExtractionJob))
    assert extraction_job.status == ExtractionJobStatus.queued
    assert extraction_job.queue_name == "starter-extraction"
    assert extraction_job.external_user_id == "external_user_123"
    assert extraction_job.max_attempts == 3
    dispatched = service.dispatch_task.calls[0][2]["args"][0]
    assert dispatched == {
        "job_id": result["job_id"],
        "tenant_id": "11111111-1111-1111-1111-111111111111",
        "queue_name": "starter-extraction",
        "_payload_reference": "extraction_job",
    }
    session.commit.assert_awaited()


@pytest.mark.asyncio
async def test_successful_dispatch_persists_broker_task_identity() -> None:
    session = FakeDispatchAsyncSession()
    cache_service = MagicMock()
    cache_service.get_idempotent_response = AsyncMock(return_value=None)
    cache_service.set_job_status = AsyncMock()
    cache_service.set_idempotent_response = AsyncMock()
    service = MemoryService(
        session=session,
        cache_service=cache_service,
        qdrant_service=MagicMock(),
        quota_manager=FakeQuotaManager(),
        proxy_user_service=SimpleNamespace(),
        dispatch_task=FakeDispatchResultTask(),
    )
    service.queue_router = SimpleNamespace(
        reserve_extraction_slot=AsyncMock(return_value=FakeReservation()),
        release_extraction_slot=AsyncMock(),
    )

    await service.queue_memory_add(
        requested_user_id=None,
        authenticated_user_id=None,
        agent_id=None,
        messages=[{"role": "user", "content": "hello"}],
        metadata={},
        idempotency_key=None,
        tenant_id="11111111-1111-1111-1111-111111111111",
        external_user_id="external_user_dispatch",
        proxy_user_id="22222222-2222-2222-2222-222222222222",
    )

    assert session.execute.await_count == 2
    assert session.commit.await_count == 2
    session.rollback.assert_not_awaited()


def test_worker_loads_compact_tenant_payload_before_processing(monkeypatch) -> None:
    full_payload = {
        "job_id": "77777777-7777-7777-7777-777777777777",
        "tenant_id": "tenant-1",
        "proxy_user_id": "proxy-1",
        "queue_name": "starter-extraction",
        "messages": [{"role": "user", "content": "private transcript"}],
    }
    captured: dict[str, object] = {}

    monkeypatch.setattr(extraction_tasks, "_set_db_job_processing", lambda **_kwargs: 0)
    monkeypatch.setattr(
        extraction_tasks,
        "_load_referenced_job_payload",
        lambda job_id: {**full_payload, "job_id": job_id},
    )
    monkeypatch.setattr(extraction_tasks, "_wait_for_development_crash_barrier", lambda **_kwargs: False)
    monkeypatch.setattr(extraction_tasks, "_set_job_status", lambda _job_id, payload: captured.setdefault("status", payload))
    monkeypatch.setattr(extraction_tasks, "_set_db_job_completed", lambda **_kwargs: None)

    def fake_pipeline(payload):
        captured["pipeline_payload"] = payload
        return {**payload, "status": "processed", "memories_created": 0}

    monkeypatch.setattr(extraction_tasks, "run_extraction_pipeline", fake_pipeline)

    result = extraction_tasks._process_extraction_job(
        {
            "job_id": full_payload["job_id"],
            "queue_name": "starter-extraction",
            "_payload_reference": "extraction_job",
        },
        celery_task_id="test-task-1",
    )

    assert captured["pipeline_payload"] == {
        **full_payload,
        "operational_metrics": {"queue_wait_ms": 0},
    }
    assert result["status"] == "processed"


def test_compact_retry_payload_preserves_queue_release_identity() -> None:
    compact = extraction_tasks._compact_task_payload(
        {
            "job_id": "job-1",
            "tenant_id": "tenant-1",
            "proxy_user_id": "proxy-1",
            "queue_name": "starter-extraction",
            "messages": [{"role": "user", "content": "private transcript"}],
        }
    )

    assert compact == {
        "job_id": "job-1",
        "tenant_id": "tenant-1",
        "queue_name": "starter-extraction",
        "_payload_reference": "extraction_job",
    }


def test_postrun_releases_legacy_compact_payload_from_result(monkeypatch) -> None:
    release = MagicMock()
    monkeypatch.setattr(extraction_tasks, "release_extraction_slot_sync", release)

    extraction_tasks.release_queue_slot_after_extraction(
        sender=SimpleNamespace(name=extraction_tasks.EXTRACTION_TASK_NAME),
        args=[
            {
                "job_id": "job-1",
                "queue_name": "starter-extraction",
                "_payload_reference": "extraction_job",
            }
        ],
        retval={
            "job_id": "job-1",
            "tenant_id": "tenant-1",
            "queue_name": "starter-extraction",
            "status": "completed",
        },
        state="SUCCESS",
    )

    release.assert_called_once_with(
        tenant_id="tenant-1",
        queue_name="starter-extraction",
        job_id="job-1",
    )


def test_worker_status_snapshot_excludes_transcript_and_memory_content() -> None:
    snapshot = extraction_tasks._job_status_snapshot(
        {
            "job_id": "job-1",
            "tenant_id": "tenant-1",
            "proxy_user_id": "proxy-1",
            "status": "processed",
            "messages": [{"role": "user", "content": "private transcript"}],
            "metadata": {"private": "data"},
            "stored_memories": [{"id": "memory-1", "content": "private memory"}],
        }
    )

    assert snapshot["result_memory_ids"] == ["memory-1"]
    assert "messages" not in snapshot
    assert "metadata" not in snapshot
    assert "stored_memories" not in snapshot


class FakeQuery:
    def __init__(self, rows):
        self.rows = rows

    def filter(self, *args, **kwargs):
        return self

    def order_by(self, *args, **kwargs):
        return self

    def limit(self, *args, **kwargs):
        return self

    def all(self):
        cutoff = datetime.now(UTC)
        return [
            row
            for row in self.rows
            if row.status == ExtractionJobStatus.processing and row.stale_after < cutoff
        ]


class FakeSyncSession:
    def __init__(self, rows):
        self.rows = rows
        self.commit_count = 0

    def query(self, _model):
        return FakeQuery(self.rows)

    def execute(self, _statement):
        for row in self.rows:
            if row.status == ExtractionJobStatus.processing:
                row.status = ExtractionJobStatus.queued
                row.stale_after = None
                row.attempts = int(row.attempts or 0) + 1
                row.updated_at = datetime.now(UTC)
                return SimpleNamespace(rowcount=1)
        return SimpleNamespace(rowcount=0)

    def commit(self):
        self.commit_count += 1

    def close(self):
        return None


class FakeQueuedQuery(FakeQuery):
    def all(self):
        cutoff = datetime.now(UTC) - watchdog_tasks.QUEUED_DISPATCH_GRACE
        return [
            row
            for row in self.rows
            if row.status == ExtractionJobStatus.queued
            and row.celery_task_id is None
            and row.processing_started_at is None
            and row.queued_at < cutoff
        ]


class FakeQueuedRecoverySession(FakeSyncSession):
    def query(self, _model):
        return FakeQueuedQuery(self.rows)

    def execute(self, statement):
        values = {
            getattr(key, "key", str(key)): getattr(value, "value", value)
            for key, value in statement._values.items()  # noqa: SLF001
        }
        for row in self.rows:
            if row.status != ExtractionJobStatus.queued:
                continue
            if "celery_task_id" in values and values["celery_task_id"] is not None:
                if row.celery_task_id is not None:
                    return SimpleNamespace(rowcount=0)
                row.celery_task_id = str(values["celery_task_id"])
                return SimpleNamespace(rowcount=1)
            if "celery_task_id" in values and values["celery_task_id"] is None:
                row.celery_task_id = None
                return SimpleNamespace(rowcount=1)
        return SimpleNamespace(rowcount=0)


def test_watchdog_requeues_stale_processing_job_only_once(monkeypatch) -> None:
    stale_job = SimpleNamespace(
        id="33333333-3333-3333-3333-333333333333",
        status=ExtractionJobStatus.processing,
        stale_after=datetime.now(UTC) - timedelta(seconds=10),
        attempts=0,
        max_attempts=3,
        tenant_id="44444444-4444-4444-4444-444444444444",
        payload={"job_id": "33333333-3333-3333-3333-333333333333"},
        queue_name="starter-extraction",
    )
    session = FakeSyncSession([stale_job])
    apply_async = MagicMock()
    monkeypatch.setattr(watchdog_tasks.process_extraction_job, "apply_async", apply_async)
    monkeypatch.setattr(watchdog_tasks, "get_extraction_queue_sync", lambda **_kwargs: "starter-extraction")

    first = watchdog_tasks.run_watchdog_cycle(session_factory=lambda: session)
    second = watchdog_tasks.run_watchdog_cycle(session_factory=lambda: session)

    assert first == {"checked": 1, "requeued": 1, "dead": 0}
    assert second == {"checked": 0, "requeued": 0, "dead": 0}
    apply_async.assert_called_once_with(
        args=[
            {
                "job_id": "33333333-3333-3333-3333-333333333333",
                "tenant_id": "44444444-4444-4444-4444-444444444444",
                "queue_name": "starter-extraction",
                "_payload_reference": "extraction_job",
            }
        ],
        queue="starter-extraction",
    )


def test_watchdog_recovers_stranded_queued_job_once_on_original_queue(monkeypatch) -> None:
    queued_job = SimpleNamespace(
        id="55555555-5555-5555-5555-555555555555",
        status=ExtractionJobStatus.queued,
        queued_at=datetime.now(UTC) - timedelta(minutes=2),
        processing_started_at=None,
        celery_task_id=None,
        attempts=0,
        max_attempts=3,
        tenant_id="66666666-6666-6666-6666-666666666666",
        payload={"job_id": "55555555-5555-5555-5555-555555555555"},
        queue_name="growth-extraction",
    )
    session = FakeQueuedRecoverySession([queued_job])
    apply_async = MagicMock()
    monkeypatch.setattr(watchdog_tasks.process_extraction_job, "apply_async", apply_async)

    first = watchdog_tasks.run_watchdog_cycle(session_factory=lambda: session)
    second = watchdog_tasks.run_watchdog_cycle(session_factory=lambda: session)

    assert first == {"checked": 1, "requeued": 1, "dead": 0}
    assert second == {"checked": 0, "requeued": 0, "dead": 0}
    assert queued_job.celery_task_id
    apply_async.assert_called_once_with(
        args=[{
            "job_id": "55555555-5555-5555-5555-555555555555",
            "tenant_id": "66666666-6666-6666-6666-666666666666",
            "queue_name": "growth-extraction",
            "_payload_reference": "extraction_job",
        }],
        queue="growth-extraction",
        task_id=queued_job.celery_task_id,
    )


def test_crash_barrier_is_disabled_for_normal_jobs(monkeypatch) -> None:
    redis_client = MagicMock()
    monkeypatch.setattr(extraction_tasks, "_redis_client", lambda: redis_client)

    assert extraction_tasks._wait_for_development_crash_barrier(
        job_id="job-1", job_payload={"metadata": {}}
    ) is False
    redis_client.set.assert_not_called()


def test_crash_barrier_is_disabled_in_production(monkeypatch) -> None:
    redis_client = MagicMock()
    monkeypatch.setattr(extraction_tasks, "_redis_client", lambda: redis_client)
    monkeypatch.setattr(
        extraction_tasks, "get_settings", lambda: SimpleNamespace(app_env="production")
    )

    assert extraction_tasks._wait_for_development_crash_barrier(
        job_id="job-2",
        job_payload={"metadata": {"_internal_celery_crash_barrier": True}},
    ) is False
    redis_client.set.assert_not_called()


def test_crash_barrier_is_one_shot_in_development(monkeypatch) -> None:
    redis_client = MagicMock()
    redis_client.set.side_effect = [True, False]
    redis_client.get.return_value = "released"
    monkeypatch.setattr(extraction_tasks, "_redis_client", lambda: redis_client)
    monkeypatch.setattr(
        extraction_tasks, "get_settings", lambda: SimpleNamespace(app_env="development")
    )
    payload={"metadata": {"_internal_celery_crash_barrier": True}}

    assert extraction_tasks._wait_for_development_crash_barrier(
        job_id="job-3", job_payload=payload
    ) is True
    assert extraction_tasks._wait_for_development_crash_barrier(
        job_id="job-3", job_payload=payload
    ) is False
    assert redis_client.set.call_count == 2
