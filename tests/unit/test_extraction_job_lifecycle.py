from __future__ import annotations

from datetime import UTC
from datetime import datetime
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
from unittest.mock import MagicMock

import pytest

from api.db.models import ExtractionJob
from api.db.models import ExtractionJobStatus
from api.db.models import QuotaMode
from api.services.memory_service import MemoryService
from api.services.quota_manager import QuotaEnvelope
from api.tasks import watchdog_tasks


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


class FakeAsyncSession:
    def __init__(self) -> None:
        self.added = []
        self.commit = AsyncMock()
        self.get = AsyncMock(return_value=None)

    def add(self, obj):
        self.added.append(obj)


class FakeReservation:
    def __init__(self) -> None:
        self.queue_name = "starter-extraction"
        self.plan_tier = "starter"


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
    session.commit.assert_awaited()


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
                "queue_name": "starter-extraction",
            }
        ],
        queue="starter-extraction",
    )
