from __future__ import annotations

import uuid
from collections import deque
from types import SimpleNamespace

from api.tasks.backfill_tasks import BackfillProxyUserIds
from api.tasks.backfill_tasks import BackfillTenantProvenance
from api.tasks.backfill_tasks import BackfillTask
from api.tasks.backfill_tasks import tenant_backfill_revision_status


class FakeScalarResult:
    def __init__(self, values):
        self._values = values

    def scalars(self):
        return self

    def all(self):
        return list(self._values)

    def scalar_one(self):
        if isinstance(self._values, list):
            return self._values[0]
        return self._values


class FakeCursorRedis:
    def __init__(self) -> None:
        self.data: dict[str, str] = {}

    def get(self, key: str):
        return self.data.get(key)

    def set(self, key: str, value: str) -> None:
        self.data[key] = value


class FakeSession:
    def __init__(self, ids_by_cursor, counts_by_cursor, active_queries: int = 0) -> None:
        self.ids_by_cursor = ids_by_cursor
        self.counts_by_cursor = counts_by_cursor
        self.active_queries = active_queries
        self.updated_batches: list[list[str]] = []
        self.progress_updates: list[dict[str, object]] = []
        self.started_jobs = 0
        self.commits = 0
        self.closed = False

    def execute(self, statement, params=None):
        sql = str(statement)
        params = params or {}
        if "INSERT INTO backfill_jobs" in sql:
            self.started_jobs += 1
            return FakeScalarResult([])
        if "UPDATE backfill_jobs" in sql:
            self.progress_updates.append(dict(params))
            return FakeScalarResult([])
        if "FROM pg_stat_activity" in sql:
            return FakeScalarResult([self.active_queries])
        if "SELECT COUNT(*)" in sql:
            cursor = params.get("cursor")
            return FakeScalarResult([self.counts_by_cursor.get(cursor, 0)])
        if "SELECT id" in sql and "FROM memories" in sql:
            cursor = params.get("cursor")
            return FakeScalarResult(self.ids_by_cursor.get(cursor, []))
        if "RETURNING m.id" in sql:
            memory_ids = list(params["memory_ids"])
            self.updated_batches.append(memory_ids)
            return FakeScalarResult(memory_ids)
        return FakeScalarResult([])

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        return None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()
        return False

    def close(self) -> None:
        self.closed = True


class FakeBackfillTask(BackfillTask):
    task_name = "fake_backfill"
    table_name = "memories"

    def __init__(self, *, session, redis_client):
        super().__init__(engine=SimpleNamespace(), redis_client=redis_client)
        self.session = session
        self.processed_batches: list[list[str]] = []

    def _where_sql(self) -> str:
        return "proxy_user_id IS NULL"

    def process_batch(self, session, batch_ids):
        self.processed_batches.append(list(batch_ids))
        return len(batch_ids)

    def run(self, batch_size=1000, sleep_between_batches_ms=100):
        return super().run(batch_size=batch_size, sleep_between_batches_ms=sleep_between_batches_ms)


def test_backfill_task_resumes_from_cursor(monkeypatch) -> None:
    ids1 = [str(uuid.uuid4()), str(uuid.uuid4())]
    ids2 = [str(uuid.uuid4())]
    redis_client = FakeCursorRedis()
    redis_client.set("backfill:fake_backfill:cursor", ids1[-1])
    session = FakeSession(
        ids_by_cursor={ids1[-1]: ids2, ids2[-1]: []},
        counts_by_cursor={ids1[-1]: 1, ids2[-1]: 0},
    )
    task = FakeBackfillTask(session=session, redis_client=redis_client)
    monkeypatch.setattr("api.tasks.backfill_tasks.Session", lambda _engine: session)
    monkeypatch.setattr(task, "_should_pause", lambda _session: False)
    monkeypatch.setattr("api.tasks.backfill_tasks.time.sleep", lambda _seconds: None)

    result = task.run(batch_size=10, sleep_between_batches_ms=0)

    assert result.status == "complete"
    assert result.processed_rows == 1
    assert task.processed_batches == [ids2]
    assert redis_client.get(task.cursor_key) == ids2[-1]


def test_backfill_task_pauses_under_load(monkeypatch) -> None:
    ids1 = [str(uuid.uuid4())]
    redis_client = FakeCursorRedis()
    session = FakeSession(
        ids_by_cursor={None: ids1, ids1[-1]: []},
        counts_by_cursor={None: 1, ids1[-1]: 0},
    )
    task = FakeBackfillTask(session=session, redis_client=redis_client)
    pauses = deque([True, False])
    monkeypatch.setattr("api.tasks.backfill_tasks.Session", lambda _engine: session)
    monkeypatch.setattr(task, "_should_pause", lambda _session: pauses.popleft() if pauses else False)
    monkeypatch.setattr("api.tasks.backfill_tasks.time.sleep", lambda _seconds: None)

    result = task.run(batch_size=10, sleep_between_batches_ms=0)

    assert result.status == "complete"
    assert result.paused_count == 1
    assert any(update["status"] == "paused" for update in session.progress_updates)


def test_backfill_proxy_user_ids_updates_only_selected_batch(monkeypatch) -> None:
    session = FakeSession(ids_by_cursor={}, counts_by_cursor={})
    task = BackfillProxyUserIds(engine=SimpleNamespace(), redis_client=FakeCursorRedis())
    batch_ids = [str(uuid.uuid4()), str(uuid.uuid4())]

    updated = task.process_batch(session, batch_ids)

    assert updated == 2
    assert session.updated_batches == [batch_ids]


def test_backfill_task_marks_failed_on_exception(monkeypatch) -> None:
    redis_client = FakeCursorRedis()
    ids1 = [str(uuid.uuid4())]
    session = FakeSession(
        ids_by_cursor={None: ids1},
        counts_by_cursor={None: 1},
    )

    class FailingBackfillTask(FakeBackfillTask):
        def process_batch(self, session, batch_ids):
            raise RuntimeError("boom")

    task = FailingBackfillTask(session=session, redis_client=redis_client)
    monkeypatch.setattr("api.tasks.backfill_tasks.Session", lambda _engine: session)
    monkeypatch.setattr(task, "_should_pause", lambda _session: False)
    monkeypatch.setattr("api.tasks.backfill_tasks.time.sleep", lambda _seconds: None)

    result = task.run(batch_size=10, sleep_between_batches_ms=0)

    assert result.status == "failed"
    assert result.error == "boom"
    assert any(update["status"] == "failed" for update in session.progress_updates)


class TenantDryRunSession:
    def __init__(self, rows):
        self.rows = rows
        self.added = []

    def execute(self, _statement):
        return SimpleNamespace(all=lambda: list(self.rows))

    def add(self, value):
        self.added.append(value)


def test_tenant_provenance_backfill_selects_only_unledgered_tenant_memories() -> None:
    task = BackfillTenantProvenance(
        dry_run=True,
        engine=SimpleNamespace(),
        redis_client=FakeCursorRedis(),
    )

    where_sql = task._where_sql()

    assert "proxy_user_id IS NOT NULL" in where_sql
    assert "memory_claim_revisions" in where_sql
    assert "mcr.memory_id = memories.id" in where_sql


def test_tenant_provenance_dry_run_does_not_write() -> None:
    memory = SimpleNamespace(id=uuid.uuid4())
    session = TenantDryRunSession([(memory, uuid.uuid4())])
    task = BackfillTenantProvenance(
        dry_run=True,
        engine=SimpleNamespace(),
        redis_client=FakeCursorRedis(),
    )

    processed = task.process_batch(session, [str(memory.id)])

    assert processed == 1
    assert session.added == []


def test_tenant_and_passport_backfills_use_separate_locks() -> None:
    task = BackfillTenantProvenance(
        dry_run=True,
        engine=SimpleNamespace(),
        redis_client=FakeCursorRedis(),
    )

    assert task.task_name == "backfill_tenant_provenance_dry_run"
    assert task.cursor_key == "backfill:backfill_tenant_provenance_dry_run:cursor"

def test_tenant_backfill_never_replaces_a_different_live_winner() -> None:
    assert tenant_backfill_revision_status(
        memory_is_archived=False,
        claim_active_value="Growth",
        incoming_value="Starter",
    ) == "superseded"


def test_tenant_backfill_asserts_matching_history_and_preserves_archives() -> None:
    assert tenant_backfill_revision_status(
        memory_is_archived=False,
        claim_active_value="Growth",
        incoming_value=" growth. ",
    ) == "asserted"
    assert tenant_backfill_revision_status(
        memory_is_archived=True,
        claim_active_value="Growth",
        incoming_value="Starter",
    ) == "archived"
