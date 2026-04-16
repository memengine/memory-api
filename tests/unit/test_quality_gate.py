from __future__ import annotations

import json
import time
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock
from unittest.mock import MagicMock

import pytest

from api.db.models import CallQualityLog
from api.db.models import OveragePolicy
from api.db.models import PlanTier
from api.db.models import TenantBudget
from api.services.budget_governor import BudgetGovernor
from api.services.quality_gate import QualityGateService


class FakeScalarResult:
    def __init__(self, item) -> None:
        self.item = item

    def scalar_one_or_none(self):
        return self.item


class FakeRedis:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}
        self.lists: dict[str, list[str]] = {}
        self.expirations: dict[str, int] = {}

    async def get(self, key: str):
        return self.values.get(key)

    async def incr(self, key: str):
        next_value = int(self.values.get(key, "0")) + 1
        self.values[key] = str(next_value)
        return next_value

    async def expire(self, key: str, ttl: int):
        self.expirations[key] = ttl
        return True

    async def lrange(self, key: str, start: int, end: int):
        items = self.lists.get(key, [])
        if end == -1:
            return items[start:]
        return items[start : end + 1]

    async def lpush(self, key: str, value: str):
        self.lists.setdefault(key, []).insert(0, value)
        return len(self.lists[key])

    async def ltrim(self, key: str, start: int, end: int):
        self.lists[key] = self.lists.get(key, [])[start : end + 1]
        return True


class FakeAioModels:
    def __init__(self, embedding: list[float]) -> None:
        self.embed_content = AsyncMock(
            return_value=SimpleNamespace(
                embeddings=[SimpleNamespace(values=embedding)]
            )
        )


class FakeGenAIClient:
    def __init__(self, embedding: list[float]) -> None:
        self.aio = SimpleNamespace(models=FakeAioModels(embedding))


def make_tenant_budget(
    *,
    monthly_call_limit: int | None = 1000,
    monthly_token_limit: int | None = 10000,
    current_month_calls: int = 0,
    current_month_tokens: int = 0,
    rate_limit_per_user_per_minute: int = 10,
    overage_policy: OveragePolicy = OveragePolicy.warn,
    alert_threshold_pct: float = 0.8,
) -> TenantBudget:
    return TenantBudget(
        id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        plan_tier=PlanTier.starter,
        monthly_call_limit=monthly_call_limit,
        monthly_token_limit=monthly_token_limit,
        current_month_calls=current_month_calls,
        current_month_tokens=current_month_tokens,
        rate_limit_per_user_per_minute=rate_limit_per_user_per_minute,
        overage_policy=overage_policy,
        alert_threshold_pct=alert_threshold_pct,
        reset_at=None,
    )


def build_service(
    *,
    tenant_budget: TenantBudget | None,
    redis_client: FakeRedis | None = None,
    embedding: list[float] | None = None,
):
    redis_client = redis_client or FakeRedis()
    cache_service = MagicMock()
    cache_service.client = redis_client
    session = MagicMock()
    session.add = MagicMock()
    session.commit = AsyncMock()
    session.rollback = AsyncMock()
    session.execute = AsyncMock(return_value=FakeScalarResult(tenant_budget))

    dispatch_task = AsyncMock()
    governor = BudgetGovernor(session=session, dispatch_task=dispatch_task)
    service = QualityGateService(
        session=session,
        cache_service=cache_service,
        budget_governor=governor,
        client=FakeGenAIClient(embedding or [1.0, 0.0]),
    )
    return service, session, redis_client, dispatch_task


@pytest.mark.asyncio
async def test_quality_gate_blocks_on_l1_rate_limit() -> None:
    tenant_id = str(uuid.uuid4())
    external_user_id = "user-1"
    budget = make_tenant_budget(rate_limit_per_user_per_minute=10)
    service, session, redis_client, _dispatch = build_service(tenant_budget=budget)
    redis_client.values[service._rate_limit_key(tenant_id, external_user_id, 12345)] = "10"

    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setattr("api.services.quality_gate.time.time", lambda: 12345 * 60)
        result = await service.check(
            [{"role": "user", "content": "How should I price my product?"}],
            tenant_id,
            external_user_id,
        )

    assert result.passed is False
    assert result.blocked_layer == "L1"
    assert result.reason == "rate_limit_exceeded"
    session.add.assert_called_once()
    logged = session.add.call_args.args[0]
    assert isinstance(logged, CallQualityLog)
    assert logged.layer_blocked_at.value == "L1"
    assert logged.reason == "rate_limit_exceeded"


@pytest.mark.asyncio
async def test_quality_gate_blocks_low_quality_conversation_on_l2() -> None:
    service, session, _redis_client, _dispatch = build_service(
        tenant_budget=make_tenant_budget()
    )

    result = await service.check(
        [{"role": "user", "content": "hi"}],
        str(uuid.uuid4()),
        "user-2",
    )

    assert result.passed is False
    assert result.blocked_layer == "L2"
    assert result.reason == "low_quality"
    logged = session.add.call_args.args[0]
    assert logged.quality_score < 0.35
    assert logged.reason == "low_quality"


@pytest.mark.asyncio
async def test_quality_gate_blocks_duplicate_query_on_l3() -> None:
    tenant_id = str(uuid.uuid4())
    external_user_id = "user-3"
    budget = make_tenant_budget()
    service, session, _redis, _dispatch = build_service(
        tenant_budget=budget,
        embedding=[1.0, 0.0],
    )

    first_result = await service.check(
        [
            {"role": "user", "content": "I need help with pricing strategy."},
            {"role": "user", "content": "What pricing model should I use for my SaaS?"},
        ],
        tenant_id,
        external_user_id,
    )
    second_result = await service.check(
        [
            {"role": "user", "content": "I need help with pricing strategy."},
            {"role": "user", "content": "What pricing model should I use for my SaaS?"},
        ],
        tenant_id,
        external_user_id,
    )

    assert first_result.passed is True
    assert second_result.passed is False
    assert second_result.blocked_layer == "L3"
    assert second_result.reason == "duplicate_query"
    assert len(session.add.call_args_list) == 2
    last_logged = session.add.call_args_list[-1].args[0]
    assert last_logged.semantic_similarity == pytest.approx(1.0)


@pytest.mark.asyncio
async def test_quality_gate_blocks_duplicate_query_on_l3_with_existing_embedding() -> None:
    tenant_id = str(uuid.uuid4())
    external_user_id = "user-3"
    budget = make_tenant_budget()
    redis_client = FakeRedis()
    existing_key = f"user_recent_queries:{tenant_id}:{external_user_id}"
    redis_client.lists[existing_key] = [
        json.dumps({"query": "pricing question", "embedding": [1.0, 0.0]})
    ]
    service, session, _redis, _dispatch = build_service(
        tenant_budget=budget,
        redis_client=redis_client,
        embedding=[1.0, 0.0],
    )

    result = await service.check(
        [{"role": "user", "content": "pricing question again?"}],
        tenant_id,
        external_user_id,
    )

    assert result.passed is False
    assert result.blocked_layer == "L3"
    assert result.reason == "duplicate_query"
    assert redis_client.expirations[existing_key] == 3600
    logged = session.add.call_args.args[0]
    assert logged.semantic_similarity == pytest.approx(1.0)


@pytest.mark.asyncio
async def test_quality_gate_blocks_on_11th_call_for_same_tenant_and_user() -> None:
    tenant_id = str(uuid.uuid4())
    external_user_id = "user-11"
    budget = make_tenant_budget(rate_limit_per_user_per_minute=10)
    service, session, _redis_client, _dispatch = build_service(tenant_budget=budget)
    service._semantic_deduplication = AsyncMock(return_value=None)

    messages = [
        {"role": "user", "content": "I am building a B2B product for AI teams."},
        {"role": "assistant", "content": "What is the biggest challenge right now?"},
        {"role": "user", "content": "Tenant isolation and cost control are the main concerns."},
    ]

    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setattr("api.services.quality_gate.time.time", lambda: 12345 * 60)
        last_result = None
        for _ in range(11):
            last_result = await service.check(messages, tenant_id, external_user_id)

    assert last_result is not None
    assert last_result.passed is False
    assert last_result.blocked_layer == "L1"
    assert last_result.reason == "rate_limit_exceeded"
    assert len(session.add.call_args_list) == 11


@pytest.mark.asyncio
async def test_quality_gate_blocks_budget_exhaustion_on_l4() -> None:
    budget = make_tenant_budget(
        monthly_call_limit=2,
        current_month_calls=2,
        overage_policy=OveragePolicy.block,
        alert_threshold_pct=1.1,
    )
    service, session, _redis_client, dispatch = build_service(tenant_budget=budget)

    result = await service.check(
        [
            {"role": "user", "content": "I am building a FastAPI service."},
            {"role": "user", "content": "How should I handle auth and deployments?"},
        ],
        str(budget.tenant_id),
        "user-4",
    )

    assert result.passed is False
    assert result.blocked_layer == "L4"
    assert result.reason == "budget_exhausted"
    assert dispatch.await_count == 0
    logged = session.add.call_args.args[0]
    assert logged.layer_blocked_at.value == "L4"


@pytest.mark.asyncio
async def test_quality_gate_full_pass_through_dispatches_usage_increment() -> None:
    budget = make_tenant_budget(
        monthly_call_limit=100,
        monthly_token_limit=100000,
        current_month_calls=10,
        current_month_tokens=100,
        alert_threshold_pct=0.99,
    )
    service, session, redis_client, dispatch = build_service(tenant_budget=budget)
    tenant_id = str(budget.tenant_id)
    external_user_id = "user-5"

    result = await service.check(
        [
            {"role": "user", "content": "I am building a production B2B AI dashboard."},
            {"role": "assistant", "content": "What are your biggest concerns?"},
            {"role": "user", "content": "Cost control, user isolation, and memory governance?"},
            {"role": "user", "content": "I also need quota-aware fallbacks and auditability."},
            {"role": "user", "content": "Can you help me design the safest rollout plan?"},
        ],
        tenant_id,
        external_user_id,
    )

    assert result.passed is True
    assert result.blocked_layer is None
    assert result.reason is None
    assert any(key.startswith(f"rate:{tenant_id}:{external_user_id}:") for key in redis_client.values)
    dispatch.assert_awaited_once()
    task_name, args = dispatch.await_args.args
    assert task_name == "api.tasks.quality_gate_tasks.increment_tenant_budget_usage"
    assert args[0] == tenant_id
    logged = session.add.call_args.args[0]
    assert logged.layer_blocked_at.value == "NONE"
    assert logged.reason is None


@pytest.mark.asyncio
async def test_quality_gate_dispatches_budget_alert_when_threshold_reached() -> None:
    budget = make_tenant_budget(
        monthly_call_limit=10,
        current_month_calls=8,
        monthly_token_limit=100000,
        current_month_tokens=100,
        alert_threshold_pct=0.8,
    )
    service, _session, _redis_client, dispatch = build_service(tenant_budget=budget)

    result = await service.check(
        [
            {"role": "user", "content": "I am designing tenant budgets for a B2B AI memory system."},
            {"role": "assistant", "content": "What should the guardrails do?"},
            {"role": "user", "content": "They should warn before overage and protect us from wasteful calls."},
        ],
        str(budget.tenant_id),
        "user-alert",
    )

    assert result.passed is True
    assert dispatch.await_count == 2
    first_task_name, first_args = dispatch.await_args_list[0].args
    second_task_name, second_args = dispatch.await_args_list[1].args
    assert first_task_name == "api.tasks.quality_gate_tasks.send_budget_alert"
    assert first_args[0] == str(budget.tenant_id)
    assert first_args[1] >= 0.8
    assert second_task_name == "api.tasks.quality_gate_tasks.increment_tenant_budget_usage"
    assert second_args[0] == str(budget.tenant_id)


@pytest.mark.asyncio
async def test_quality_gate_performance_for_l1_only_is_under_five_seconds() -> None:
    budget = make_tenant_budget(rate_limit_per_user_per_minute=0)
    service, session, _redis_client, _dispatch = build_service(tenant_budget=budget)
    messages = [
        {"role": "user", "content": "I am building a production B2B memory platform."},
        {"role": "assistant", "content": "What do you need help with?"},
        {"role": "user", "content": "Quota enforcement, audit logs, and safe defaults."},
    ]

    started_at = time.perf_counter()
    for index in range(1000):
        await service.check(messages, str(uuid.uuid4()), f"user-{index}")
    elapsed = time.perf_counter() - started_at

    assert elapsed < 5.0
    assert len(session.add.call_args_list) == 1000
