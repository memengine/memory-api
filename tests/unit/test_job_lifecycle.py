from __future__ import annotations

import json
from datetime import UTC
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock
from unittest.mock import MagicMock

import pytest

from api.db.models import DeadLetterJob
from api.db.models import ExtractionJob
from api.db.models import ExtractionJobStatus
from api.db.models import QuotaMode
from api.services.memory_service import MemoryService
from api.services.quota_manager import QuotaEnvelope
from api.tasks import extraction_tasks


class FakeQuotaManager:
    def __init__(self) -> None:
        self.get_quota_envelope = AsyncMock(
            return_value=QuotaEnvelope(mode=QuotaMode.full, budget_remaining_pct=0.9, reset_at=None)
        )


class FakeDispatchTask:
    def __call__(self, task_name, *args, **kwargs):
        return None


class FakeAsyncSession:
    def __init__(self) -> None:
        self.added = []
        self.commit = AsyncMock()
        self.get = AsyncMock(return_value=None)

    def add(self, obj):
        self.added.append(obj)


class FakeReservation:
    queue_name = "starter-extraction"
    plan_tier = "starter"


@pytest.mark.asyncio
async def test_queue_memory_add_sets_default_max_attempts() -> None:
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

    await service.queue_memory_add(
        requested_user_id=None,
        authenticated_user_id=None,
        agent_id=None,
        messages=[{"role": "user", "content": "hello"}],
        metadata={},
        idempotency_key=None,
        tenant_id="11111111-1111-1111-1111-111111111111",
        external_user_id="ext_user_1",
        proxy_user_id="22222222-2222-2222-2222-222222222222",
    )

    row = next(obj for obj in session.added if isinstance(obj, ExtractionJob))
    assert row.max_attempts == 3


class FakeDeadLetterQuery:
    def __init__(self, session):
        self.session = session

    def filter(self, *_args, **_kwargs):
        return self

    def one_or_none(self):
        return self.session.dead_letter


class FakeFailureSession:
    def __init__(self, attempts: int, max_attempts: int) -> None:
        self.dead_letter = None
        self.job = SimpleNamespace(
            id="33333333-3333-3333-3333-333333333333",
            tenant_id="11111111-1111-1111-1111-111111111111",
            proxy_user_id="22222222-2222-2222-2222-222222222222",
            celery_task_id="celery_1",
            attempts=attempts,
            max_attempts=max_attempts,
            status=ExtractionJobStatus.processing,
            error=None,
            stale_after=datetime.now(UTC),
            dead_lettered_at=None,
            payload={"job_id": "33333333-3333-3333-3333-333333333333"},
        )

    def get(self, _model, _identifier):
        return self.job

    def query(self, model):
        if model is DeadLetterJob:
            return FakeDeadLetterQuery(self)
        raise AssertionError("Unexpected query model")

    def add(self, obj):
        if isinstance(obj, DeadLetterJob):
            self.dead_letter = obj

    def commit(self):
        return None

    def close(self):
        return None


class FakeProcessingResult:
    def __init__(self, job) -> None:
        self.job = job

    def scalar_one_or_none(self):
        return self.job


class FakeProcessingSession:
    def __init__(self, *, stored_task_id: str | None) -> None:
        self.job = SimpleNamespace(
            status=ExtractionJobStatus.queued,
            celery_task_id=stored_task_id,
            attempts=0,
            processing_started_at=None,
            started_at=None,
            stale_after=None,
            completed_at=None,
            updated_at=None,
            error=None,
            error_type=None,
        )
        self.commits = 0
        self.rollbacks = 0

    def execute(self, _statement):
        return FakeProcessingResult(self.job)

    def add(self, _job):
        return None

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1

    def close(self):
        return None


def test_late_original_task_cannot_replace_watchdog_recovery_lease(monkeypatch) -> None:
    session = FakeProcessingSession(stored_task_id="recovery-task")
    monkeypatch.setattr(extraction_tasks, "build_extraction_session_factory", lambda: (lambda: session))

    result = extraction_tasks._set_db_job_processing(  # noqa: SLF001
        job_id="33333333-3333-3333-3333-333333333333",
        celery_task_id="late-original-task",
    )

    assert result is None
    assert session.job.status == ExtractionJobStatus.queued
    assert session.job.celery_task_id == "recovery-task"
    assert session.rollbacks == 1


def test_watchdog_recovery_task_claims_reserved_job(monkeypatch) -> None:
    session = FakeProcessingSession(stored_task_id="recovery-task")
    monkeypatch.setattr(extraction_tasks, "build_extraction_session_factory", lambda: (lambda: session))

    result = extraction_tasks._set_db_job_processing(  # noqa: SLF001
        job_id="33333333-3333-3333-3333-333333333333",
        celery_task_id="recovery-task",
    )

    assert result == 0
    assert session.job.status == ExtractionJobStatus.processing
    assert session.commits == 1


def test_set_db_job_failure_moves_to_dead_and_creates_dead_letter(monkeypatch) -> None:
    session = FakeFailureSession(attempts=2, max_attempts=3)
    monkeypatch.setattr(extraction_tasks, "build_extraction_session_factory", lambda: (lambda: session))
    sentry_calls = []
    monkeypatch.setattr(extraction_tasks.sentry_sdk, "capture_message", lambda *args, **kwargs: sentry_calls.append((args, kwargs)))

    status, attempts, max_attempts = extraction_tasks._set_db_job_failure(  # noqa: SLF001
        job_id="33333333-3333-3333-3333-333333333333",
        error="Proxy user 123 not found.",
    )

    assert status == ExtractionJobStatus.dead
    assert attempts == 3
    assert max_attempts == 3
    assert session.job.status == ExtractionJobStatus.dead
    assert session.job.error == "proxy_user_not_found"
    assert session.dead_letter is not None
    assert session.dead_letter.error == "proxy_user_not_found"
    assert sentry_calls


def test_pipeline_failure_preserves_safe_stage_and_cause() -> None:
    error = extraction_tasks.ExtractionPipelineError(
        stage="store_memories",
        cause=ValueError("database value was invalid"),
    )

    assert extraction_tasks.classify_error(error) == "unknown_error"
    assert extraction_tasks._safe_failure_detail(error) == (  # noqa: SLF001
        "extraction_pipeline_failed stage=store_memories cause=ValueError"
    )
    assert extraction_tasks._normalize_stored_error(  # noqa: SLF001
        extraction_tasks._safe_failure_detail(error)  # noqa: SLF001
    ) == "extraction_pipeline_failed stage=store_memories cause=ValueError"


def test_pipeline_failure_records_only_safe_database_diagnostics() -> None:
    error = extraction_tasks.ExtractionPipelineError(
        stage="store_memories",
        cause=SimpleNamespace(
            orig=SimpleNamespace(
                sqlstate="23503",
                diag=SimpleNamespace(constraint_name="fk_memories_embedding_model_id"),
            )
        ),
    )

    assert extraction_tasks._safe_failure_detail(error) == (  # noqa: SLF001
        "extraction_pipeline_failed stage=store_memories cause=SimpleNamespace "
        "sqlstate=23503 constraint=fk_memories_embedding_model_id"
    )


def test_json_decode_error_is_classified_as_invalid_llm_response() -> None:
    error = extraction_tasks.ExtractionPipelineError(
        stage="extract_memories",
        cause=json.JSONDecodeError("invalid JSON", "{", 1),
    )

    assert extraction_tasks.classify_error(error) == "llm_invalid_response"


def test_extraction_session_factory_is_reused_per_process(monkeypatch) -> None:
    first_factory = SimpleNamespace(kw={"bind": MagicMock()})
    second_factory = SimpleNamespace(kw={"bind": MagicMock()})
    factories = iter([first_factory, second_factory])
    build_calls: list[int] = []

    extraction_tasks.dispose_extraction_session_factory()
    monkeypatch.setattr(extraction_tasks.os, "getpid", lambda: 101)
    monkeypatch.setattr(
        extraction_tasks,
        "build_sync_session_factory",
        lambda: build_calls.append(1) or next(factories),
    )

    assert extraction_tasks.build_extraction_session_factory() is first_factory
    assert extraction_tasks.build_extraction_session_factory() is first_factory
    assert len(build_calls) == 1

    monkeypatch.setattr(extraction_tasks.os, "getpid", lambda: 202)
    assert extraction_tasks.build_extraction_session_factory() is second_factory
    assert len(build_calls) == 2
    first_factory.kw["bind"].dispose.assert_called_once_with()

    extraction_tasks.dispose_extraction_session_factory()
    second_factory.kw["bind"].dispose.assert_called_once_with()
