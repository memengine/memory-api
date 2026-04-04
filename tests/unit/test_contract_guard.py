from __future__ import annotations

import pytest

from api.db.migrations.contract_guard import assert_backfill_completed
from api.db.migrations.contract_guard import assert_no_remaining_nulls


class _ScalarResult:
    def __init__(self, values):
        self._values = values

    def scalar_one(self):
        return self._values

    def scalars(self):
        return self

    def all(self):
        return list(self._values)


class _MappingsResult:
    def __init__(self, value):
        self._value = value

    def mappings(self):
        return self

    def first(self):
        return self._value


class FakeBind:
    def __init__(self, *, null_count: int = 0, sample_ids: list[str] | None = None, backfill_row=None) -> None:
        self.null_count = null_count
        self.sample_ids = sample_ids or []
        self.backfill_row = backfill_row

    def execute(self, statement, params=None):
        sql = str(statement)
        if "SELECT COUNT(*)" in sql and "IS NULL" in sql:
            return _ScalarResult(self.null_count)
        if "LIMIT :limit" in sql and "IS NULL" in sql:
            return _ScalarResult(self.sample_ids)
        if "FROM backfill_jobs" in sql:
            return _MappingsResult(self.backfill_row)
        raise AssertionError(f"Unexpected SQL: {sql}")


def test_assert_no_remaining_nulls_passes_when_clean() -> None:
    bind = FakeBind(null_count=0)
    assert_no_remaining_nulls(
        bind,
        table_name="memories",
        column_name="proxy_user_id",
    )


def test_assert_no_remaining_nulls_raises_with_clear_message() -> None:
    bind = FakeBind(null_count=3, sample_ids=["id-1", "id-2"])
    with pytest.raises(RuntimeError, match="Contract migration blocked"):
        assert_no_remaining_nulls(
            bind,
            table_name="memories",
            column_name="proxy_user_id",
        )


def test_assert_backfill_completed_passes_when_latest_job_complete() -> None:
    bind = FakeBind(
        backfill_row={"status": "complete", "processed_rows": 10, "total_rows": 10}
    )
    assert_backfill_completed(bind, task_name="backfill_proxy_user_ids")


def test_assert_backfill_completed_raises_when_missing_or_incomplete() -> None:
    missing = FakeBind(backfill_row=None)
    with pytest.raises(RuntimeError, match="no backfill_jobs row found"):
        assert_backfill_completed(missing, task_name="backfill_proxy_user_ids")

    running = FakeBind(
        backfill_row={"status": "running", "processed_rows": 2, "total_rows": 10}
    )
    with pytest.raises(RuntimeError, match="status='running'"):
        assert_backfill_completed(running, task_name="backfill_proxy_user_ids")
