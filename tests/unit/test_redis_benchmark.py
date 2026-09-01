from __future__ import annotations

from unittest.mock import Mock

from api.infra.redis_benchmark import benchmark_async_redis_from_url
from api.infra.redis_benchmark import benchmark_timeout_seconds
from api.infra.redis_benchmark import redis_benchmark_enabled


def test_redis_benchmark_overrides_require_all_isolation_markers(monkeypatch) -> None:
    monkeypatch.setenv("BENCHMARK_REDIS_CONNECT_TIMEOUT_MS", "500")
    monkeypatch.setenv("APP_ENV", "development")
    monkeypatch.setenv("MEMORYOS_SCALE_DEDICATED", "1")
    monkeypatch.setenv("MEMORYOS_BENCHMARK_PROVIDER", "deterministic")

    assert redis_benchmark_enabled() is False
    assert benchmark_timeout_seconds("BENCHMARK_REDIS_CONNECT_TIMEOUT_MS", 0.1) == 0.1


def test_redis_benchmark_timeout_override_is_milliseconds(monkeypatch) -> None:
    monkeypatch.setenv("APP_ENV", "benchmark")
    monkeypatch.setenv("MEMORYOS_SCALE_DEDICATED", "1")
    monkeypatch.setenv("MEMORYOS_BENCHMARK_PROVIDER", "deterministic")
    monkeypatch.setenv("BENCHMARK_REDIS_CIRCUIT_DEADLINE_MS", "500")

    assert redis_benchmark_enabled() is True
    assert benchmark_timeout_seconds("BENCHMARK_REDIS_CIRCUIT_DEADLINE_MS", 0.2) == 0.5


def test_normal_redis_client_uses_approved_production_timeouts(monkeypatch) -> None:
    monkeypatch.delenv("APP_ENV", raising=False)
    factory = Mock(return_value=object())
    monkeypatch.setattr("api.infra.redis_benchmark.redis.from_url", factory)

    benchmark_async_redis_from_url(
        "redis://localhost:6379/0",
        encoding="utf-8",
        decode_responses=True,
        socket_connect_timeout=0.5,
        socket_timeout=0.5,
    )

    kwargs = factory.call_args.kwargs
    assert kwargs["socket_connect_timeout"] == 0.5
    assert kwargs["socket_timeout"] == 0.5
    assert kwargs["retry_on_timeout"] is False


def test_normal_redis_client_preserves_omitted_timeout_and_retry_options(monkeypatch) -> None:
    monkeypatch.delenv("APP_ENV", raising=False)
    factory = Mock(return_value=object())
    monkeypatch.setattr("api.infra.redis_benchmark.redis.from_url", factory)

    benchmark_async_redis_from_url(
        "redis://localhost:6379/0",
        encoding="utf-8",
        decode_responses=True,
        client_role="cache",
    )

    assert factory.call_args.kwargs == {
        "encoding": "utf-8",
        "decode_responses": True,
    }


def test_dedicated_benchmark_can_override_approved_defaults(monkeypatch) -> None:
    monkeypatch.setenv("APP_ENV", "benchmark")
    monkeypatch.setenv("MEMORYOS_SCALE_DEDICATED", "1")
    monkeypatch.setenv("MEMORYOS_BENCHMARK_PROVIDER", "deterministic")
    monkeypatch.setenv("BENCHMARK_REDIS_CONNECT_TIMEOUT_MS", "250")
    monkeypatch.setenv("BENCHMARK_REDIS_SOCKET_TIMEOUT_MS", "300")
    pool_factory = Mock(return_value=object())
    client_factory = Mock(return_value=object())
    monkeypatch.setattr("api.infra.redis_benchmark.BenchmarkConnectionPool.from_url", pool_factory)
    monkeypatch.setattr("api.infra.redis_benchmark.redis.Redis", client_factory)

    benchmark_async_redis_from_url(
        "redis://localhost:6379/0",
        encoding="utf-8",
        decode_responses=True,
        socket_connect_timeout=0.5,
        socket_timeout=0.5,
        client_role="auth",
    )

    kwargs = pool_factory.call_args.kwargs
    assert kwargs["socket_connect_timeout"] == 0.25
    assert kwargs["socket_timeout"] == 0.3
    assert kwargs["retry_on_timeout"] is False
    assert kwargs["benchmark_client_role"] == "auth"


def test_dedicated_benchmark_preserves_omitted_timeout_and_retry_options(monkeypatch) -> None:
    monkeypatch.setenv("APP_ENV", "benchmark")
    monkeypatch.setenv("MEMORYOS_SCALE_DEDICATED", "1")
    monkeypatch.setenv("MEMORYOS_BENCHMARK_PROVIDER", "deterministic")
    monkeypatch.setenv("BENCHMARK_REDIS_CONNECT_TIMEOUT_MS", "250")
    monkeypatch.setenv("BENCHMARK_REDIS_SOCKET_TIMEOUT_MS", "300")
    pool_factory = Mock(return_value=object())
    monkeypatch.setattr("api.infra.redis_benchmark.BenchmarkConnectionPool.from_url", pool_factory)
    monkeypatch.setattr("api.infra.redis_benchmark.redis.Redis", Mock(return_value=object()))

    benchmark_async_redis_from_url(
        "redis://localhost:6379/0",
        encoding="utf-8",
        decode_responses=True,
        client_role="cache",
    )

    assert pool_factory.call_args.kwargs == {
        "benchmark_client_role": "cache",
        "connection_class": pool_factory.call_args.kwargs["connection_class"],
        "encoding": "utf-8",
        "decode_responses": True,
    }
