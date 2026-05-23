from __future__ import annotations

import uuid
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
from unittest.mock import MagicMock

import pytest

from api.db.models import Memory
from api.db.models import MemoryCategory
from api.db.models import QuotaMode
from api.services.embedding_service import DEFAULT_ACTIVE_MODEL_ID
from api.services.lifecycle_manager import MemoryLifecycleManager
from api.services.retriever import RetrieverService
from api.tasks.lifecycle_tasks import is_peak_ist_window
from api.tasks.scoring_tasks import LIFECYCLE_TASK_BEAT_SCHEDULE


class FakeScalarResult:
    def __init__(self, items):
        self._items = list(items)

    def scalars(self):
        return self

    def all(self):
        return list(self._items)

    def scalar_one(self):
        return self._items[0] if self._items else 0


class FakeSession:
    def __init__(self, execute_results):
        self.execute_results = list(execute_results)
        self.added = []
        self.flushes = 0
        self.commits = 0

    async def execute(self, _statement):
        return FakeScalarResult(self.execute_results.pop(0))

    def add(self, item):
        self.added.append(item)

    async def flush(self):
        self.flushes += 1

    async def commit(self):
        self.commits += 1


class FakeCache:
    def __init__(self):
        self.hot_memories = []
        self.hot_tier_payloads = []
        self.reports = []

    async def set_hot_tier_memory(self, proxy_user_id, memory_id, memory, ttl=86400):
        self.hot_memories.append(
            {
                "proxy_user_id": proxy_user_id,
                "memory_id": memory_id,
                "memory": memory,
                "ttl": ttl,
            }
        )

    async def set_lifecycle_report(self, tenant_id, report):
        self.reports.append({"tenant_id": tenant_id, "report": report})

    async def get_hot_tier_memories(self, _proxy_user_id):
        return list(self.hot_tier_payloads)

    async def get_hot_memories(self, _user_id):
        return None

    async def set_hot_memories(self, *_args, **_kwargs):
        return None


class FakeQdrant:
    def __init__(self):
        self.deleted_memory_ids = []

    def delete_memory(self, memory_id):
        self.deleted_memory_ids.append(memory_id)
        return True


def make_memory(
    *,
    importance_score: float,
    access_count: int = 0,
    last_accessed_at: datetime | None = None,
    is_archived: bool = False,
    category: MemoryCategory = MemoryCategory.preference,
) -> Memory:
    return Memory(
        id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        proxy_user_id=uuid.uuid4(),
        content="Stored lifecycle memory",
        category=category,
        importance_score=importance_score,
        confidence_score=0.9,
        embedding_id=str(uuid.uuid4()),
        embedding_model_id=DEFAULT_ACTIVE_MODEL_ID,
        source_conversation_id=uuid.uuid4(),
        previous_version_id=None,
        expires_at=None,
        metadata_json={},
        access_count=access_count,
        last_accessed_at=last_accessed_at or datetime.now(UTC),
        is_archived=is_archived,
    )


@pytest.mark.asyncio
async def test_lifecycle_decays_archives_promotes_and_reports() -> None:
    now = datetime(2026, 4, 25, 20, 30, tzinfo=UTC)
    inactive_memory = make_memory(
        importance_score=5.0,
        last_accessed_at=now - timedelta(days=75),
    )
    archive_memory = make_memory(
        importance_score=1.2,
        last_accessed_at=now - timedelta(days=100),
    )
    hot_memory = make_memory(
        importance_score=8.5,
        access_count=6,
        last_accessed_at=now - timedelta(days=2),
    )
    session = FakeSession(
        [
            [inactive_memory],
            [archive_memory],
            [],
            [hot_memory],
            [inactive_memory, archive_memory, hot_memory],
        ]
    )
    cache = FakeCache()
    qdrant = FakeQdrant()

    report = await MemoryLifecycleManager(
        session=session,
        cache_service=cache,
        qdrant_service=qdrant,
        now=now,
        enforce_off_peak=False,
    ).run_for_tenant(str(uuid.uuid4()))

    assert report.decayed_count == 1
    assert inactive_memory.importance_score < 5.0
    assert inactive_memory.metadata_json["original_importance_score"] == 5.0
    assert report.archived_count == 1
    assert archive_memory.is_archived is True
    assert qdrant.deleted_memory_ids == [str(archive_memory.id)]
    assert report.promoted_to_hot == 1
    assert cache.hot_memories[0]["memory_id"] == str(hot_memory.id)
    assert cache.hot_memories[0]["ttl"] == 86400
    assert report.rescored_count == 3
    assert session.commits == 1
    assert cache.reports


@pytest.mark.asyncio
async def test_lifecycle_decays_five_inactive_memories() -> None:
    now = datetime(2026, 4, 25, 20, 30, tzinfo=UTC)
    inactive_memories = [
        make_memory(
            importance_score=5.0 + index,
            last_accessed_at=now - timedelta(days=45),
        )
        for index in range(5)
    ]
    original_scores = [memory.importance_score for memory in inactive_memories]
    session = FakeSession([inactive_memories, [], [], inactive_memories])

    report = await MemoryLifecycleManager(
        session=session,
        cache_service=FakeCache(),
        qdrant_service=FakeQdrant(),
        now=now,
        enforce_off_peak=False,
    ).run_for_tenant(str(uuid.uuid4()))

    assert report.decayed_count == 5
    assert all(
        memory.importance_score < original_score
        for memory, original_score in zip(inactive_memories, original_scores, strict=True)
    )


@pytest.mark.asyncio
async def test_lifecycle_skips_peak_ist_window() -> None:
    peak_ist_now = datetime(2026, 4, 25, 6, 0, tzinfo=UTC)
    session = FakeSession([])
    cache = FakeCache()

    report = await MemoryLifecycleManager(
        session=session,
        cache_service=cache,
        now=peak_ist_now,
    ).run_for_tenant(str(uuid.uuid4()))

    assert report.skipped is True
    assert report.reason == "peak_hours_ist"
    assert session.commits == 0
    assert cache.reports


def test_decay_is_idempotent_from_metadata_baseline() -> None:
    now = datetime(2026, 4, 25, 20, 30, tzinfo=UTC)
    memory = make_memory(
        importance_score=4.0,
        last_accessed_at=now - timedelta(days=120),
    )
    manager = MemoryLifecycleManager(
        session=FakeSession([]),
        cache_service=FakeCache(),
        now=now,
        enforce_off_peak=False,
    )

    first = manager.importance_scorer.compute_decay(memory, now=now)
    memory.importance_score = first
    second = manager.importance_scorer.compute_decay(memory, now=now)

    assert second == first


@pytest.mark.asyncio
async def test_hot_tier_memory_is_returned_without_qdrant_lookup(monkeypatch) -> None:
    proxy_user_id = str(uuid.uuid4())
    hot_memory = make_memory(
        importance_score=8.5,
        access_count=10,
        last_accessed_at=datetime.now(UTC),
    )
    hot_memory.proxy_user_id = uuid.UUID(proxy_user_id)
    cache = FakeCache()
    cache.hot_tier_payloads = [MemoryLifecycleManager._memory_cache_payload(hot_memory)]
    session = MagicMock()
    session.execute = AsyncMock(return_value=FakeScalarResult([10]))
    qdrant_service = MagicMock()
    qdrant_service.search_memories = MagicMock(return_value=[])
    qdrant_service.breaker = SimpleNamespace(current_state=lambda: "CLOSED")
    quota_manager = SimpleNamespace(get_mode=AsyncMock(return_value=QuotaMode.full))
    task_mock = MagicMock()
    monkeypatch.setattr("api.services.retriever.update_memory_accesses", task_mock)

    results = await RetrieverService(
        session=session,
        qdrant_service=qdrant_service,
        cache_service=cache,
        quota_manager=quota_manager,
        proxy_user_service=SimpleNamespace(),
    ).retrieve(
        query="anything",
        external_user_id="student-1",
        proxy_user_id=proxy_user_id,
        tenant_id=str(uuid.uuid4()),
        limit=1,
    )

    assert [result.id for result in results] == [str(hot_memory.id)]
    qdrant_service.search_memories.assert_not_called()


def test_lifecycle_schedule_runs_sunday_two_utc() -> None:
    schedule = LIFECYCLE_TASK_BEAT_SCHEDULE["run-weekly-memory-lifecycle"]

    assert schedule["task"] == "api.tasks.scoring_tasks.run_weekly_memory_lifecycle"
    assert str(schedule["schedule"]) == "<crontab: 0 2 * * sun (m/h/dM/MY/d)>"


def test_lifecycle_task_peak_window_uses_utc_plus_530() -> None:
    assert is_peak_ist_window(datetime(2026, 5, 10, 3, 30, tzinfo=UTC)) is True
    assert is_peak_ist_window(datetime(2026, 5, 10, 16, 29, tzinfo=UTC)) is True
    assert is_peak_ist_window(datetime(2026, 5, 10, 16, 30, tzinfo=UTC)) is False
    assert is_peak_ist_window(datetime(2026, 5, 10, 20, 0, tzinfo=UTC)) is False
