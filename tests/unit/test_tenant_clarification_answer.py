from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from starlette.requests import Request

from api.db.models import ClarificationQueueStatus, CrossUserConflictStatus
from api.errors import APIError
from api.routers.memories import answer_memory_clarification
from api.schemas.requests import MemoryClarificationAnswerRequest


class _ScalarResult:
    def __init__(self, value) -> None:
        self.value = value

    def scalar_one_or_none(self):
        return self.value


class _Session:
    def __init__(self, *results) -> None:
        self.results = iter(results)
        self.statements = []
        self.commits = 0

    async def execute(self, statement):
        self.statements.append(statement)
        return _ScalarResult(next(self.results))

    async def commit(self) -> None:
        self.commits += 1


class _ProxyUserService:
    def __init__(self, proxy_user_id: uuid.UUID | None) -> None:
        self.proxy_user_id = proxy_user_id
        self.calls = []

    async def find_existing(self, *, tenant_id: str, external_user_id: str):
        self.calls.append((tenant_id, external_user_id))
        return SimpleNamespace(id=self.proxy_user_id) if self.proxy_user_id is not None else None


def _request() -> Request:
    request = Request({"type": "http", "method": "POST", "path": "/", "headers": []})
    request.state.request_id = "test-request-id"
    return request


def _clarification(*, proxy_user_id: uuid.UUID, conflict_id: uuid.UUID, **overrides):
    values = {
        "id": uuid.uuid4(),
        "tenant_id": uuid.uuid4(),
        "proxy_user_id": proxy_user_id,
        "conflict_id": conflict_id,
        "status": ClarificationQueueStatus.triggered,
        "expires_at": datetime.now(UTC) + timedelta(hours=1),
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _conflict(conflict_id: uuid.UUID, **overrides):
    values = {
        "id": conflict_id,
        "status": CrossUserConflictStatus.pending,
        "resolution_path": "user_session",
        "resolved_at": None,
        "resolved_by": None,
        "resolution": None,
        "resolution_reason": None,
        "requires_attention": True,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


@pytest.mark.asyncio
async def test_tenant_chat_can_resolve_its_own_clarification(monkeypatch) -> None:
    tenant_id = uuid.uuid4()
    proxy_user_id = uuid.uuid4()
    conflict_id = uuid.uuid4()
    clarification = _clarification(proxy_user_id=proxy_user_id, conflict_id=conflict_id)
    conflict = _conflict(conflict_id)
    session = _Session(clarification, conflict)
    proxy_service = _ProxyUserService(proxy_user_id)
    selection_calls = []

    async def fake_apply(session_arg, *, conflict, selection, changed_by, reason):
        selection_calls.append((session_arg, conflict.id, selection, changed_by, reason))

    monkeypatch.setattr("api.routers.memories.apply_conflict_selection", fake_apply)

    response = await answer_memory_clarification(
        request=_request(),
        clarification_id=str(clarification.id),
        payload=MemoryClarificationAnswerRequest(
            external_user_id="customer-user-1",
            answer="B",
        ),
        proxy_user_service=proxy_service,
        session=session,
        tenant_id=str(tenant_id),
    )

    assert response.data.resolved is True
    assert response.data.resolution == "B"
    assert selection_calls == [
        (
            session,
            conflict_id,
            "B",
            "user",
            "User confirmed memory B in the customer chat.",
        )
    ]
    assert clarification.status == ClarificationQueueStatus.resolved
    assert conflict.status == CrossUserConflictStatus.resolved
    assert conflict.resolved_by == "user_session"
    assert session.commits == 1
    assert proxy_service.calls == [(str(tenant_id), "customer-user-1")]
    assert all("FOR UPDATE" in str(statement) for statement in session.statements)


@pytest.mark.asyncio
async def test_tenant_chat_does_not_create_identity_for_unknown_external_user() -> None:
    session = _Session()

    with pytest.raises(APIError) as exc_info:
        await answer_memory_clarification(
            request=_request(),
            clarification_id=str(uuid.uuid4()),
            payload=MemoryClarificationAnswerRequest(
                external_user_id="mistyped-user",
                answer="A",
            ),
            proxy_user_service=_ProxyUserService(None),
            session=session,
            tenant_id=str(uuid.uuid4()),
        )

    assert exc_info.value.status_code == 404
    assert exc_info.value.error == "clarification_not_found"
    assert session.statements == []
    assert session.commits == 0


@pytest.mark.asyncio
async def test_tenant_chat_cannot_answer_another_user_clarification() -> None:
    tenant_id = uuid.uuid4()
    proxy_user_id = uuid.uuid4()
    session = _Session(None)

    with pytest.raises(APIError) as exc_info:
        await answer_memory_clarification(
            request=_request(),
            clarification_id=str(uuid.uuid4()),
            payload=MemoryClarificationAnswerRequest(
                external_user_id="customer-user-2",
                answer="A",
            ),
            proxy_user_service=_ProxyUserService(proxy_user_id),
            session=session,
            tenant_id=str(tenant_id),
        )

    assert exc_info.value.status_code == 404
    assert exc_info.value.error == "clarification_not_found"
    sql = str(session.statements[0])
    assert "clarification_queue.tenant_id" in sql
    assert "clarification_queue.proxy_user_id" in sql
    assert "FOR UPDATE" in sql
    assert session.commits == 0


@pytest.mark.asyncio
async def test_tenant_chat_rejects_expired_clarification() -> None:
    proxy_user_id = uuid.uuid4()
    clarification = _clarification(
        proxy_user_id=proxy_user_id,
        conflict_id=uuid.uuid4(),
        expires_at=datetime.now(UTC) - timedelta(seconds=1),
    )
    session = _Session(clarification)

    with pytest.raises(APIError) as exc_info:
        await answer_memory_clarification(
            request=_request(),
            clarification_id=str(clarification.id),
            payload=MemoryClarificationAnswerRequest(
                external_user_id="customer-user-1",
                answer="A",
            ),
            proxy_user_service=_ProxyUserService(proxy_user_id),
            session=session,
            tenant_id=str(uuid.uuid4()),
        )

    assert exc_info.value.status_code == 409
    assert exc_info.value.error == "clarification_expired"
    assert clarification.status == ClarificationQueueStatus.expired
    assert session.commits == 1


@pytest.mark.asyncio
async def test_tenant_chat_rejects_duplicate_resolution() -> None:
    proxy_user_id = uuid.uuid4()
    conflict_id = uuid.uuid4()
    clarification = _clarification(
        proxy_user_id=proxy_user_id,
        conflict_id=conflict_id,
        status=ClarificationQueueStatus.resolved,
    )
    session = _Session(clarification)

    with pytest.raises(APIError) as exc_info:
        await answer_memory_clarification(
            request=_request(),
            clarification_id=str(clarification.id),
            payload=MemoryClarificationAnswerRequest(
                external_user_id="customer-user-1",
                answer="both",
            ),
            proxy_user_service=_ProxyUserService(proxy_user_id),
            session=session,
            tenant_id=str(uuid.uuid4()),
        )

    assert exc_info.value.status_code == 409
    assert exc_info.value.error == "clarification_already_resolved"
    assert session.commits == 0


@pytest.mark.asyncio
async def test_tenant_chat_rejects_tenant_review_conflict(monkeypatch) -> None:
    proxy_user_id = uuid.uuid4()
    conflict_id = uuid.uuid4()
    clarification = _clarification(proxy_user_id=proxy_user_id, conflict_id=conflict_id)
    conflict = _conflict(conflict_id, resolution_path="tenant_review")
    session = _Session(clarification, conflict)

    async def fail_if_called(*_args, **_kwargs):
        raise AssertionError("selection must not run for tenant-review conflicts")

    monkeypatch.setattr("api.routers.memories.apply_conflict_selection", fail_if_called)

    with pytest.raises(APIError) as exc_info:
        await answer_memory_clarification(
            request=_request(),
            clarification_id=str(clarification.id),
            payload=MemoryClarificationAnswerRequest(
                external_user_id="customer-user-1",
                answer="neither",
            ),
            proxy_user_service=_ProxyUserService(proxy_user_id),
            session=session,
            tenant_id=str(uuid.uuid4()),
        )

    assert exc_info.value.status_code == 400
    assert exc_info.value.error == "conflict_not_user_session"
    assert session.commits == 0
