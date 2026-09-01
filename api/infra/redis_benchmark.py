from __future__ import annotations

import json
import logging
import os
import time
from itertools import count
from typing import Any

import redis.asyncio as redis
from redis.asyncio.connection import Connection
from redis.asyncio.connection import ConnectionPool
from redis.asyncio.retry import Retry
from redis.backoff import NoBackoff


LOGGER = logging.getLogger("memoryos.redis_benchmark")
_SAMPLE_COUNTER = count()


def redis_benchmark_enabled() -> bool:
    return (
        os.getenv("APP_ENV", "").strip().lower() == "benchmark"
        and os.getenv("MEMORYOS_SCALE_DEDICATED") == "1"
        and os.getenv("MEMORYOS_BENCHMARK_PROVIDER") == "deterministic"
    )


def benchmark_cache_generations_enabled() -> bool:
    return (
        redis_benchmark_enabled()
        and os.getenv("BENCHMARK_CACHE_INVALIDATION_MODE", "").strip().lower()
        == "generation-v1"
        and os.getenv("BENCHMARK_CACHE_NAMESPACE", "").strip() == "v2"
    )


def benchmark_redis_tcp_preflight_bypassed() -> bool:
    if redis_benchmark_enabled():
        return (
            os.getenv("BENCHMARK_REDIS_TCP_PREFLIGHT", "enabled").strip().lower()
            == "disabled"
        )
    return os.getenv("REDIS_TCP_PREFLIGHT_ENABLED", "false").strip().lower() not in {
        "1",
        "true",
        "yes",
        "on",
    }


def benchmark_timeout_seconds(name: str, default: float) -> float:
    if not redis_benchmark_enabled():
        return default
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    return max(0.001, float(raw) / 1000.0)


def emit_benchmark_timing(metric: str, started_at: float, **fields: Any) -> None:
    if fields.get("outcome") != "error" and next(_SAMPLE_COUNTER) % 10 != 0:
        return
    LOGGER.warning(
        json.dumps(
            {
                "event": "redis_benchmark_timing",
                "metric": metric,
                "captured_at_unix_ms": round(time.time() * 1000.0, 3),
                "latency_ms": round((time.perf_counter() - started_at) * 1000.0, 3),
                **fields,
            },
            sort_keys=True,
        )
    )


class BenchmarkConnection(Connection):
    async def connect(self) -> None:
        started_at = time.perf_counter()
        created = not self.is_connected
        try:
            await super().connect()
        except Exception as exc:
            emit_benchmark_timing("connection", started_at, client_role=getattr(self, "_benchmark_client_role", "unknown"), created=created, outcome="error", reason=type(exc).__name__)
            raise
        emit_benchmark_timing("connection", started_at, client_role=getattr(self, "_benchmark_client_role", "unknown"), created=created, outcome="ok")

    async def send_command(self, *args: Any, **kwargs: Any) -> None:
        self._benchmark_command_started_at = time.perf_counter()
        self._benchmark_command_name = str(args[0]) if args else "unknown"
        await super().send_command(*args, **kwargs)

    async def read_response(self, *args: Any, **kwargs: Any) -> Any:
        started_at = getattr(self, "_benchmark_command_started_at", time.perf_counter())
        command = getattr(self, "_benchmark_command_name", "unknown")
        try:
            result = await super().read_response(*args, **kwargs)
        except Exception as exc:
            emit_benchmark_timing("command", started_at, client_role=getattr(self, "_benchmark_client_role", "unknown"), command=command, outcome="error", reason=type(exc).__name__)
            raise
        emit_benchmark_timing("command", started_at, client_role=getattr(self, "_benchmark_client_role", "unknown"), command=command, outcome="ok")
        return result


class BenchmarkConnectionPool(ConnectionPool):
    def __init__(self, *args: Any, benchmark_client_role: str = "unknown", **kwargs: Any) -> None:
        self.benchmark_client_role = benchmark_client_role
        super().__init__(*args, **kwargs)

    def make_connection(self) -> Connection:
        connection = super().make_connection()
        connection._benchmark_client_role = self.benchmark_client_role
        return connection

    async def get_connection(self, command_name=None, *keys: Any, **options: Any):
        started_at = time.perf_counter()
        try:
            connection = await super().get_connection(command_name, *keys, **options)
        except Exception as exc:
            emit_benchmark_timing(
                "pool_acquisition",
                started_at,
                client_role=self.benchmark_client_role,
                available_connections=len(self._available_connections),
                in_use_connections=len(self._in_use_connections),
                max_connections=self.max_connections,
                outcome="error",
                reason=type(exc).__name__,
            )
            raise
        emit_benchmark_timing(
            "pool_acquisition",
            started_at,
            client_role=self.benchmark_client_role,
            available_connections=len(self._available_connections),
            in_use_connections=len(self._in_use_connections),
            max_connections=self.max_connections,
            outcome="ok",
        )
        return connection


def benchmark_async_redis_from_url(
    url: str,
    *,
    encoding: str,
    decode_responses: bool,
    socket_connect_timeout: float | None = None,
    socket_timeout: float | None = None,
    client_role: str = "unknown",
) -> redis.Redis:
    connection_options: dict[str, object] = {
        "encoding": encoding,
        "decode_responses": decode_responses,
    }
    if socket_connect_timeout is not None:
        connection_options["socket_connect_timeout"] = benchmark_timeout_seconds(
            "BENCHMARK_REDIS_CONNECT_TIMEOUT_MS",
            socket_connect_timeout,
        )
    if socket_timeout is not None:
        connection_options["socket_timeout"] = benchmark_timeout_seconds(
            "BENCHMARK_REDIS_SOCKET_TIMEOUT_MS",
            socket_timeout,
        )
    if socket_connect_timeout is not None or socket_timeout is not None:
        connection_options["retry"] = Retry(NoBackoff(), 0)
        connection_options["retry_on_timeout"] = False
    if not redis_benchmark_enabled():
        if socket_connect_timeout is not None:
            connection_options["socket_connect_timeout"] = socket_connect_timeout
        if socket_timeout is not None:
            connection_options["socket_timeout"] = socket_timeout
        return redis.from_url(url, **connection_options)
    pool = BenchmarkConnectionPool.from_url(
        url,
        benchmark_client_role=client_role,
        connection_class=BenchmarkConnection,
        **connection_options,
    )
    return redis.Redis(connection_pool=pool)


__all__ = [
    "benchmark_async_redis_from_url",
    "benchmark_cache_generations_enabled",
    "benchmark_redis_tcp_preflight_bypassed",
    "benchmark_timeout_seconds",
    "emit_benchmark_timing",
    "redis_benchmark_enabled",
]
