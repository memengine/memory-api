from __future__ import annotations

import hashlib
import hmac
import json
import uuid
from collections import deque
from datetime import UTC
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from api.db.models import OveragePolicy
from api.db.models import PlanTier
from api.db.models import TenantBudget
from api.db.models import QuotaMode
from api.services.webhook_event_service import WEBHOOK_EVENT_TASK_NAME
from api.services.webhook_event_service import WebhookEventService


class FakeScalarResult:
    def __init__(self, item) -> None:
        self.item = item

    def scalar_one_or_none(self):
        return self.item


class FakeSyncSession:
    def __init__(self, budget: TenantBudget | None) -> None:
        self.budget = budget
        self.commits = 0

    def execute(self, _query):
        return FakeScalarResult(self.budget)

    def add(self, _item) -> None:
        return None

    def commit(self) -> None:
        self.commits += 1

    def refresh(self, _item) -> None:
        return None

    def close(self) -> None:
        return None


class FakeClient:
    def __init__(self, outcomes: list[object]) -> None:
        self.outcomes = deque(outcomes)
        self.calls: list[dict] = []

    def __enter__(self):
        return self

    def __exit__(self, _exc_type, _exc, _tb) -> None:
        return None

    def post(self, url: str, *, content: bytes, headers: dict[str, str]):
        self.calls.append({"url": url, "content": content, "headers": headers})
        outcome = self.outcomes.popleft()
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def make_budget(**overrides) -> TenantBudget:
    base = dict(
        id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        plan_tier=PlanTier.starter,
        monthly_call_limit=1000,
        monthly_token_limit=10000,
        current_month_calls=100,
        current_month_tokens=100,
        write_calls=0,
        write_call_limit=100,
        read_calls=0,
        read_limit=100,
        rate_limit_per_user_per_minute=10,
        overage_policy=OveragePolicy.warn,
        alert_threshold_pct=0.8,
        alert_webhook_url="https://tenant.example.test/webhook",
        webhook_secret="supersecret",
        last_notified_pct=None,
        last_notified_mode=QuotaMode.full,
        reset_at=datetime.now(UTC),
        created_at=datetime.now(UTC),
    )
    base.update(overrides)
    return TenantBudget(**base)


def test_send_skips_silently_when_no_webhook_url() -> None:
    session = FakeSyncSession(make_budget(alert_webhook_url=None))
    client = FakeClient([])
    service = WebhookEventService(
        session_factory=lambda: session,
        client_factory=lambda: client,
    )

    service.send(str(session.budget.tenant_id), "quota.warning", {"remaining_pct": 0.2})

    assert client.calls == []


def test_send_skips_invalid_webhook_url_without_raising() -> None:
    session = FakeSyncSession(make_budget(alert_webhook_url="http://127.0.0.1/internal"))
    client = FakeClient([])
    service = WebhookEventService(
        session_factory=lambda: session,
        client_factory=lambda: client,
    )

    service.send(str(session.budget.tenant_id), "quota.warning", {"remaining_pct": 0.2})

    assert client.calls == []


def test_send_posts_signed_payload_and_retries_on_non_2xx(monkeypatch) -> None:
    budget = make_budget()
    session = FakeSyncSession(budget)
    failure = SimpleNamespace(status_code=503)
    success = SimpleNamespace(status_code=200)
    client = FakeClient([failure, success])
    monkeypatch.setattr("api.services.webhook_event_service.time.sleep", lambda _seconds: None)
    service = WebhookEventService(
        session_factory=lambda: session,
        client_factory=lambda: client,
    )

    service.send(str(budget.tenant_id), "quota.warning", {"remaining_pct": 0.2})

    assert len(client.calls) == 2
    first_call = client.calls[0]
    payload = json.loads(first_call["content"].decode("utf-8"))
    assert payload["event"] == "quota.warning"
    assert payload["tenant_id"] == str(budget.tenant_id)
    expected_signature = hmac.new(
        budget.webhook_secret.encode("utf-8"),
        first_call["content"],
        hashlib.sha256,
    ).hexdigest()
    assert first_call["headers"]["X-MemoryOS-Signature"] == expected_signature
    assert first_call["headers"]["X-MemoryOS-Event"] == "quota.warning"


@pytest.mark.asyncio
async def test_send_quota_warning_dispatches_celery_task() -> None:
    budget = make_budget()
    session = SimpleNamespace(
        execute=AsyncMock(return_value=FakeScalarResult(budget)),
    )
    dispatch_task = AsyncMock()
    service = WebhookEventService(
        session=session,
        dispatch_task=dispatch_task,
    )

    await service.send_quota_warning(str(budget.tenant_id), 0.2, 0.8)

    task_name, args = dispatch_task.await_args.args
    assert task_name == WEBHOOK_EVENT_TASK_NAME
    assert args[0] == str(budget.tenant_id)
    assert args[1] == "quota.warning"
