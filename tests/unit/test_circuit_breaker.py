from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from api.infra.circuit_breaker import CircuitBreaker
from api.infra.circuit_breaker import CircuitOpenError


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


def test_circuit_transitions_from_closed_to_open_after_threshold(fake_clock: FakeClock) -> None:
    breaker = build_breaker(FakeStateClient())

    for _ in range(3):
        with pytest.raises(RuntimeError):
            breaker.call_sync(lambda: (_ for _ in ()).throw(RuntimeError("boom")))

    assert breaker.current_state() == "OPEN"


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

