from __future__ import annotations

from unittest.mock import Mock
from types import SimpleNamespace
import json
from pathlib import Path

from api.infra.postgres_benchmark import instrument_engine
from api.infra.postgres_benchmark import postgres_benchmark_enabled
from api.infra.postgres_benchmark import _statement_shape
from api.infra.postgres_benchmark import _driver_state
from benchmarks.internal.postgres_connection_observer import sanitized_query_shape
from api.db.database import CircuitBreakerAsyncSession
from benchmarks.internal.postgres_connection_observer import require_safe_environment
from benchmarks.internal.postgres_connection_observer import database_url_from_compose


def test_postgres_benchmark_requires_all_isolation_markers(monkeypatch) -> None:
    monkeypatch.setenv("APP_ENV", "development")
    monkeypatch.setenv("MEMORYOS_SCALE_DEDICATED", "1")
    monkeypatch.setenv("MEMORYOS_BENCHMARK_PROVIDER", "deterministic")

    assert postgres_benchmark_enabled() is False


def test_statement_shape_is_bounded_and_excludes_parameters() -> None:
    statement = "SELECT * FROM memories WHERE tenant_id = $1 AND content = $2 ORDER BY created_at DESC"
    shape = _statement_shape(statement)
    assert len(shape) <= 240
    assert "secret-memory-value" not in shape
    assert "$1" in shape


def test_observer_query_shape_redacts_literals() -> None:
    shape = sanitized_query_shape("UPDATE memories SET content='private value' WHERE id=123")
    assert "private value" not in shape
    assert "123" not in shape
    assert "UPDATE memories" in shape


def test_session_lifecycle_instrumentation_is_benchmark_gated(monkeypatch) -> None:
    monkeypatch.setattr("api.db.database.postgres_benchmark_enabled", lambda: False)
    session = CircuitBreakerAsyncSession()
    assert "memoryos_benchmark_session_id" not in session.info


def test_driver_state_reports_backend_pid_and_transaction() -> None:
    class Driver:
        def get_server_pid(self): return 4321
        def is_in_transaction(self): return True
        def is_closed(self): return False

    class AdaptedConnection:
        driver_connection = Driver()

    assert _driver_state(AdaptedConnection()) == {
        "backend_pid": 4321,
        "driver_in_transaction": True,
        "driver_closed": False,
    }


def test_engine_instrumentation_is_noop_outside_benchmark(monkeypatch) -> None:
    monkeypatch.delenv("APP_ENV", raising=False)
    engine = Mock()

    assert instrument_engine(engine, kind="sync", owner="test") is engine
    assert engine.pool.mock_calls == []


def test_postgres_observer_accepts_only_dedicated_scale_database(monkeypatch) -> None:
    monkeypatch.setenv("APP_ENV", "benchmark")
    monkeypatch.setenv("MEMORYOS_SCALE_DEDICATED", "1")
    monkeypatch.setenv(
        "DATABASE_URL",
        "postgresql+asyncpg://memoryos_scale:local@127.0.0.1:15432/memoryos_scale",
    )

    require_safe_environment()


def test_benchmark_engine_labels_new_connections(monkeypatch) -> None:
    monkeypatch.setenv("APP_ENV", "benchmark")
    monkeypatch.setenv("MEMORYOS_SCALE_DEDICATED", "1")
    monkeypatch.setenv("MEMORYOS_BENCHMARK_PROVIDER", "deterministic")
    monkeypatch.setattr("api.infra.postgres_benchmark.event.listens_for", lambda *_args: lambda fn: fn)
    engine = Mock()
    engine.pool.size.return_value = 5
    engine.pool._max_overflow = 10

    assert instrument_engine(engine, kind="sync", owner="test.owner") is engine


def test_postgres_observer_derives_disposable_compose_credentials(monkeypatch) -> None:
    config = {
        "services": {
            "postgres": {
                "environment": {
                    "POSTGRES_USER": "memoryos_scale",
                    "POSTGRES_PASSWORD": "local test password",
                    "POSTGRES_DB": "memoryos_scale",
                },
                "ports": [{"target": 5432, "published": "15432"}],
            }
        }
    }
    monkeypatch.setattr(
        "benchmarks.internal.postgres_connection_observer.subprocess.run",
        lambda *_args, **_kwargs: SimpleNamespace(returncode=0, stdout=json.dumps(config)),
    )

    url = database_url_from_compose(
        project="memoryos-scale",
        compose_file=Path("docker-compose.scale.yml"),
        env_file=Path(".env.scale"),
    )

    assert url == "postgresql+asyncpg://memoryos_scale:local%20test%20password@127.0.0.1:15432/memoryos_scale"
