from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from api.infra.circuit_breaker import CircuitBreaker
from api.infra.circuit_breaker import CircuitOpenError
from api.infra.redis_benchmark import benchmark_redis_tcp_preflight_bypassed


class FakeStateClient:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}

    def get(self, key: str) -> str | None:
        return self.values.get(key)

    def set(self, key: str, value: str) -> bool:
        self.values[key] = value
        return True


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def time(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def fake_clock(monkeypatch: pytest.MonkeyPatch) -> FakeClock:
    clock = FakeClock()
    monkeypatch.setattr("api.infra.circuit_breaker.time.time", clock.time)
    return clock


def build_breaker(state_client: FakeStateClient) -> CircuitBreaker:
    return CircuitBreaker(
        name="redis",
        failure_threshold=3,
        window_seconds=10,
        recovery_timeout_seconds=5,
        state_client=state_client,
    )


def test_redis_breaker_uses_approved_production_deadline(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("APP_ENV", raising=False)
    breaker = build_breaker(FakeStateClient())

    assert breaker._execution_timeout_seconds == 0.75


def test_dedicated_benchmark_can_override_redis_deadline(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APP_ENV", "benchmark")
    monkeypatch.setenv("MEMORYOS_SCALE_DEDICATED", "1")
    monkeypatch.setenv("MEMORYOS_BENCHMARK_PROVIDER", "deterministic")
    monkeypatch.setenv("BENCHMARK_REDIS_CIRCUIT_DEADLINE_MS", "500")
    breaker = build_breaker(FakeStateClient())

    assert breaker._execution_timeout_seconds == 0.5


def test_redis_circuit_derives_caller_role_from_instrumented_pool() -> None:
    class InstrumentedClient:
        connection_pool = type("Pool", (), {"benchmark_client_role": "auth"})()

        async def get(self) -> None:
            return None

    assert CircuitBreaker._benchmark_client_role(InstrumentedClient().get) == "auth"
    assert CircuitBreaker._benchmark_client_role(lambda: None) == "unknown"


def test_circuit_transitions_from_closed_to_open_after_threshold(fake_clock: FakeClock) -> None:
    breaker = build_breaker(FakeStateClient())

    for _ in range(3):
        with pytest.raises(RuntimeError):
            breaker.call_sync(lambda: (_ for _ in ()).throw(RuntimeError("boom")))

    assert breaker.current_state() == "OPEN"


def test_external_failures_obey_threshold(fake_clock: FakeClock) -> None:
    breaker = build_breaker(FakeStateClient())

    for _ in range(2):
        breaker.record_external_failure(
            source="auth_outer_deadline",
            client_role="auth",
            reason="outer_deadline",
            operation="api_key_cache_lookup",
        )

    assert breaker.current_state() == "CLOSED"
    assert breaker.snapshot()["failure_count"] == 2

    breaker.record_external_failure(
        source="auth_outer_deadline",
        client_role="auth",
        reason="outer_deadline",
        operation="api_key_cache_lookup",
    )
    assert breaker.current_state() == "OPEN"


def test_external_failure_does_not_restart_open_recovery_clock(fake_clock: FakeClock) -> None:
    breaker = build_breaker(FakeStateClient())
    for _ in range(3):
        breaker.record_external_failure(source="auth_outer_deadline")
    opened_at = breaker.snapshot()["opened_at"]

    fake_clock.advance(2)
    breaker.record_external_failure(source="auth_outer_deadline")

    assert breaker.snapshot()["opened_at"] == opened_at


def test_open_state_rejects_fast_without_calling_function(fake_clock: FakeClock) -> None:
    breaker = build_breaker(FakeStateClient())

    for _ in range(3):
        with pytest.raises(RuntimeError):
            breaker.call_sync(lambda: (_ for _ in ()).throw(RuntimeError("boom")))

    called = False

    def should_not_run() -> None:
        nonlocal called
        called = True

    with pytest.raises(CircuitOpenError):
        breaker.call_sync(should_not_run)

    assert called is False


def test_open_transitions_to_half_open_then_closed_on_success(fake_clock: FakeClock) -> None:
    breaker = build_breaker(FakeStateClient())

    for _ in range(3):
        with pytest.raises(RuntimeError):
            breaker.call_sync(lambda: (_ for _ in ()).throw(RuntimeError("boom")))

    fake_clock.advance(6)
    result = breaker.call_sync(lambda: "ok")

    assert result == "ok"
    assert breaker.current_state() == "CLOSED"


def test_half_open_transitions_back_to_open_on_failure(fake_clock: FakeClock) -> None:
    breaker = build_breaker(FakeStateClient())

    for _ in range(3):
        with pytest.raises(RuntimeError):
            breaker.call_sync(lambda: (_ for _ in ()).throw(RuntimeError("boom")))

    fake_clock.advance(6)
    with pytest.raises(RuntimeError):
        breaker.call_sync(lambda: (_ for _ in ()).throw(RuntimeError("still down")))

    assert breaker.current_state() == "OPEN"


@pytest.mark.asyncio
async def test_async_breaker_uses_shared_state_client(fake_clock: FakeClock) -> None:
    state_client = FakeStateClient()
    first = CircuitBreaker(
        name="gemini_embed",
        failure_threshold=2,
        window_seconds=10,
        recovery_timeout_seconds=5,
        state_client=state_client,
    )
    second = CircuitBreaker(
        name="gemini_embed",
        failure_threshold=2,
        window_seconds=10,
        recovery_timeout_seconds=5,
        state_client=state_client,
    )

    failing_call = AsyncMock(side_effect=RuntimeError("down"))
    with pytest.raises(RuntimeError):
        await first.call(failing_call)
    with pytest.raises(RuntimeError):
        await first.call(failing_call)

    assert first.current_state() == "OPEN"
    assert second.current_state() == "OPEN"


@pytest.mark.asyncio
async def test_single_redis_preflight_failure_does_not_force_open(
    fake_clock: FakeClock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    breaker = build_breaker(FakeStateClient())
    monkeypatch.setattr(
        breaker,
        "_redis_connectivity_preflight",
        AsyncMock(return_value=(False, True)),
    )

    result = await breaker.call(AsyncMock(), fallback=lambda: "fallback")

    assert result == "fallback"
    assert breaker.current_state() == "CLOSED"
    assert breaker.snapshot()["failure_count"] == 1


@pytest.mark.asyncio
async def test_repeated_redis_preflight_failures_open_at_threshold(
    fake_clock: FakeClock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    breaker = build_breaker(FakeStateClient())
    monkeypatch.setattr(
        breaker,
        "_redis_connectivity_preflight",
        AsyncMock(return_value=(False, True)),
    )

    for _ in range(2):
        assert await breaker.call(AsyncMock(), fallback=lambda: "fallback") == "fallback"
        assert breaker.current_state() == "CLOSED"

    assert await breaker.call(AsyncMock(), fallback=lambda: "fallback") == "fallback"
    assert breaker.current_state() == "OPEN"


@pytest.mark.asyncio
async def test_cached_negative_redis_preflight_is_not_counted_twice(
    fake_clock: FakeClock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    breaker = build_breaker(FakeStateClient())
    monkeypatch.setattr(
        breaker,
        "_redis_connectivity_preflight",
        AsyncMock(side_effect=[(False, True), (False, False), (False, False)]),
    )

    for _ in range(3):
        assert await breaker.call(AsyncMock(), fallback=lambda: "fallback") == "fallback"

    assert breaker.current_state() == "CLOSED"
    assert breaker.snapshot()["failure_count"] == 1


@pytest.mark.asyncio
async def test_command_driven_redis_health_is_the_normal_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("REDIS_TCP_PREFLIGHT_ENABLED", raising=False)
    assert benchmark_redis_tcp_preflight_bypassed() is True

    monkeypatch.setenv("REDIS_TCP_PREFLIGHT_ENABLED", "true")
    assert benchmark_redis_tcp_preflight_bypassed() is False


@pytest.mark.asyncio
async def test_dedicated_benchmark_preflight_override_is_independent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("REDIS_TCP_PREFLIGHT_ENABLED", "false")

    monkeypatch.setenv("APP_ENV", "benchmark")
    monkeypatch.setenv("MEMORYOS_SCALE_DEDICATED", "1")
    monkeypatch.setenv("MEMORYOS_BENCHMARK_PROVIDER", "deterministic")
    monkeypatch.delenv("BENCHMARK_REDIS_TCP_PREFLIGHT", raising=False)
    assert benchmark_redis_tcp_preflight_bypassed() is False

    monkeypatch.setenv("BENCHMARK_REDIS_TCP_PREFLIGHT", "disabled")
    assert benchmark_redis_tcp_preflight_bypassed() is True


@pytest.mark.asyncio
async def test_bypassed_preflight_uses_real_command_failures_for_open_and_recovery(
    fake_clock: FakeClock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("APP_ENV", "benchmark")
    monkeypatch.setenv("MEMORYOS_SCALE_DEDICATED", "1")
    monkeypatch.setenv("MEMORYOS_BENCHMARK_PROVIDER", "deterministic")
    monkeypatch.setenv("BENCHMARK_REDIS_TCP_PREFLIGHT", "disabled")
    breaker = build_breaker(FakeStateClient())
    failing_call = AsyncMock(side_effect=RuntimeError("redis unavailable"))

    for _ in range(3):
        with pytest.raises(RuntimeError, match="redis unavailable"):
            await breaker.call(failing_call)

    assert breaker.current_state() == "OPEN"
    assert await breaker.call(AsyncMock(), fallback=lambda: "fallback") == "fallback"

    fake_clock.advance(6)
    assert await breaker.call(AsyncMock(return_value="recovered")) == "recovered"
    assert breaker.current_state() == "CLOSED"
