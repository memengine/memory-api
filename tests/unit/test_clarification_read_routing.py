from __future__ import annotations

import uuid

import pytest

from api.routers.memories import _pop_next_clarification_question


class _EmptyResult:
    def scalar_one_or_none(self):
        return None


class _CapturingSession:
    def __init__(self) -> None:
        self.statement = None

    async def execute(self, statement):
        self.statement = statement
        return _EmptyResult()


@pytest.mark.asyncio
async def test_next_session_read_only_selects_user_session_conflicts() -> None:
    session = _CapturingSession()

    result = await _pop_next_clarification_question(
        session=session,
        proxy_user_id=str(uuid.uuid4()),
    )

    sql = str(session.statement)
    assert result is None
    assert "LEFT OUTER JOIN cross_user_conflicts" in sql
    assert "cross_user_conflicts.resolution_path" in sql
    assert "clarification_queue.conflict_id IS NULL" in sql
