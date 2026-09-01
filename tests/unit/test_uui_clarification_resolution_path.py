from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest
from starlette.requests import Request

from api.db.models import ClarificationQueueStatus
from api.db.models import CrossUserConflictStatus
from api.errors import APIError
from api.routers.uui import answer_my_clarification
from api.schemas.uui_schemas import ClarificationAnswerRequest


class _ScalarResult:
    def __init__(self, value):
        self.value = value

    def scalars(self):
        return self

    def all(self):
        return self.value

    def scalar_one_or_none(self):
        return self.value


class _Session:
    def __init__(self, proxy_user_id: uuid.UUID, clarification) -> None:
        self.results = iter(
            [
                _ScalarResult([proxy_user_id]),
                _ScalarResult(clarification),
            ]
        )
        self.committed = False

    async def execute(self, _statement):
        return next(self.results)

    async def commit(self):
        self.committed = True


@pytest.mark.asyncio
async def test_passport_answer_rejects_tenant_review_conflict() -> None:
    proxy_user_id = uuid.uuid4()
    conflict = SimpleNamespace(
        status=CrossUserConflictStatus.pending,
        resolution_path="tenant_review",
    )
    clarification = SimpleNamespace(
        id=uuid.uuid4(),
        proxy_user_id=proxy_user_id,
        status=ClarificationQueueStatus.pending,
        conflict=conflict,
    )
    session = _Session(proxy_user_id, clarification)
    request = Request({"type": "http", "method": "POST", "path": "/", "headers": []})

    with pytest.raises(APIError) as exc_info:
        await answer_my_clarification(
            request=request,
            clarification_id=str(clarification.id),
            payload=ClarificationAnswerRequest(answer="A"),
            session=session,
            universal_user=SimpleNamespace(id=uuid.uuid4()),
        )

    assert exc_info.value.status_code == 400
    assert exc_info.value.error == "conflict_not_user_session"
    assert session.committed is False
