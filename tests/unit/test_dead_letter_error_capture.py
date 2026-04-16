from __future__ import annotations

from datetime import UTC
from datetime import datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest

from api.db.models import DeadLetterJob
from api.db.models import ExtractionJobStatus
from api.routers.internal import dead_letter_jobs
from api.tasks import extraction_tasks


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
            payload={"messages": [{"role": "user", "content": "remember this"}]},
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


class FakeResult:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows


class FakeDeadLetterSession:
    def __init__(self, rows):
        self.rows = rows

    async def execute(self, _statement):
        return FakeResult(self.rows)


def test_capture_error_detail_truncates_to_last_2000_chars(monkeypatch: pytest.MonkeyPatch) -> None:
    long_traceback = "Traceback (most recent call last):\n" + ("x" * 5000)
    monkeypatch.setattr(extraction_tasks.traceback, "format_exc", lambda: long_traceback)

    error_detail = extraction_tasks._capture_error_detail()  # noqa: SLF001

    assert len(error_detail) == 2000
    assert error_detail == long_traceback[-2000:]


def test_set_db_job_failure_stores_traceback_and_payload_in_dead_letter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = FakeFailureSession(attempts=2, max_attempts=3)
    traceback_text = "Traceback (most recent call last):\nValueError: boom"

    monkeypatch.setattr(extraction_tasks, "build_extraction_session_factory", lambda: (lambda: session))
    monkeypatch.setattr(extraction_tasks.sentry_sdk, "capture_message", lambda *args, **kwargs: None)

    status, attempts, max_attempts = extraction_tasks._set_db_job_failure(  # noqa: SLF001
        job_id="33333333-3333-3333-3333-333333333333",
        error=traceback_text,
    )

    assert status == ExtractionJobStatus.dead
    assert attempts == 3
    assert max_attempts == 3
    assert session.job.error == traceback_text
    assert session.dead_letter is not None
    assert session.dead_letter.error == traceback_text
    assert session.dead_letter.payload == session.job.payload


@pytest.mark.asyncio
async def test_dead_letter_jobs_endpoint_returns_payload_and_full_error() -> None:
    job = SimpleNamespace(
        id=uuid4(),
        tenant_id=uuid4(),
        proxy_user_id=uuid4(),
        external_user_id="ajeet",
        status=ExtractionJobStatus.dead,
        attempts=3,
        queue_name="starter-extraction",
        error="Traceback (most recent call last):\nRuntimeError: broken",
        payload={"messages": [{"role": "user", "content": "remember DVC preference"}]},
        created_at=datetime.now(UTC),
        queued_at=datetime.now(UTC),
        processing_started_at=datetime.now(UTC),
        started_at=datetime.now(UTC),
        completed_at=None,
        dead_lettered_at=datetime.now(UTC),
    )
    dead_letter = SimpleNamespace(
        error="Traceback (most recent call last):\nRuntimeError: broken",
        payload={"messages": [{"role": "user", "content": "remember DVC preference"}]},
    )
    session = FakeDeadLetterSession(rows=[(dead_letter, job)])
    request = SimpleNamespace(state=SimpleNamespace(request_id="req-1"), headers={})

    response = await dead_letter_jobs(request=request, session=session)

    assert response["request_id"] == "req-1"
    assert len(response["data"]) == 1
    row = response["data"][0]
    assert row["error"].startswith("Traceback (most recent call last):")
    assert row["payload"] == {"messages": [{"role": "user", "content": "remember DVC preference"}]}
