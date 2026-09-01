from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from api.benchmark_runtime_telemetry import runtime_telemetry_enabled
from benchmarks.internal import scale_harness
from scripts.run_scale_benchmark import prepare_summary_path
from scripts.run_scale_benchmark import HostHeartbeat
from scripts.run_scale_benchmark import require_disposable_stack_ready
from scripts.analyze_runtime_correlation import parse_events
from scripts.analyze_runtime_correlation import summarize


ROOT = Path(__file__).resolve().parents[2]
WORKLOAD = ROOT / "benchmarks" / "internal" / "scale-workload-v1.json"
K6_SCRIPT = ROOT / "scripts" / "moderate_scale_k6.js"


class _Settings:
    def __init__(self, app_env: str) -> None:
        self.app_env = app_env


def _safe_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APP_ENV", "benchmark")
    monkeypatch.setenv("MEMORYOS_SCALE_DEDICATED", "1")
    monkeypatch.setenv("MEMORYOS_BENCHMARK_PROVIDER", "deterministic")
    monkeypatch.setenv("BENCHMARK_API_KEY", "not-a-real-secret")
    monkeypatch.setenv("SCALE_SOURCE_SERVICE", "scale-benchmark")
    monkeypatch.setenv("SCALE_COMPOSE_PROJECT", "memoryos-scale")
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://user:pass@postgres:5432/memoryos_scale")
    monkeypatch.setenv("REDIS_URL", "redis://redis:6379/0")
    monkeypatch.setenv("QDRANT_URL", "http://qdrant:6333")
    monkeypatch.setenv("QDRANT_COLLECTION", "scale_memories_v1")
    monkeypatch.setenv("OPENAI_API_KEY", "")
    monkeypatch.setenv("GEMINI_API_KEY", "")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")
    monkeypatch.setenv("BENCHMARK_CACHE_INVALIDATION_MODE", "generation-v1")
    monkeypatch.setenv("BENCHMARK_CACHE_NAMESPACE", "v2")
    monkeypatch.setenv("BENCHMARK_REDIS_TCP_PREFLIGHT", "enabled")


def test_scale_harness_fails_closed_for_production(monkeypatch: pytest.MonkeyPatch) -> None:
    _safe_env(monkeypatch)
    monkeypatch.setattr(scale_harness, "get_settings", lambda: _Settings("production"))
    with pytest.raises(RuntimeError, match="disabled in production"):
        scale_harness.require_safe_environment()


def test_scale_harness_requires_explicit_dedicated_marker(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(scale_harness, "get_settings", lambda: _Settings("development"))
    monkeypatch.delenv("MEMORYOS_SCALE_DEDICATED", raising=False)
    monkeypatch.setenv("BENCHMARK_API_KEY", "not-a-real-secret")
    monkeypatch.setenv("SCALE_SOURCE_SERVICE", "scale-benchmark")
    with pytest.raises(RuntimeError, match="disposable dedicated stack"):
        scale_harness.require_safe_environment()


def test_scale_harness_rejects_paid_provider_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    _safe_env(monkeypatch)
    monkeypatch.setattr(scale_harness, "get_settings", lambda: _Settings("benchmark"))
    monkeypatch.setenv("OPENAI_API_KEY", "must-not-be-used")
    with pytest.raises(RuntimeError, match="OPENAI_API_KEY must be empty"):
        scale_harness.require_safe_environment()


def test_frozen_workload_is_development_only_and_sums_to_100() -> None:
    workload = json.loads(WORKLOAD.read_text(encoding="utf-8"))
    assert workload["classification"] == "development"
    assert workload["holdout_allowed"] is False
    assert workload["production_allowed"] is False
    assert sum(workload["traffic"].values()) == 100
    assert workload["stages"]["LOW"] == {
        "virtual_users": 5,
        "requests_per_second": 2,
        "duration": "10m",
    }
    assert workload["hard_caps"]["max_requests"] == 100000


def test_k6_runner_has_safety_caps_and_current_api_contracts() -> None:
    script = K6_SCRIPT.read_text(encoding="utf-8")
    assert 'MEMORYOS_SCALE_DEDICATED !== "1"' in script
    assert 'STAGE !== "LOW" && __ENV.APPROVE_NON_LOW !== "1"' in script
    assert 'post("/v1/memories/add"' in script
    assert 'post("/v1/memories/retrieve"' in script
    assert "/v1/memories/jobs/${jobId}" in script
    assert "MAX_REQUESTS" in script
    assert 'DIAGNOSTIC_1RPS: { vus: 4, rate: 1, duration: "3m" }' in script
    assert 'DIAGNOSTIC_2RPS: { vus: 5, rate: 2, duration: "3m" }' in script
    assert 'summaryTrendStats: ["avg", "min", "med", "p(90)", "p(95)", "p(99)", "max"]' in script
    assert "holdout" not in script.lower()
    assert "K6_SUMMARY_PATH" in script


def test_scale_launcher_prepares_run_scoped_summary_directory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("scripts.run_scale_benchmark.ROOT", tmp_path)

    output = prepare_summary_path("diagnostic-safe-1", "DIAGNOSTIC_2RPS")

    assert output == tmp_path / "artifacts/internal-benchmarks/scale/diagnostic-safe-1/k6-diagnostic_2rps.json"
    assert output.parent.is_dir()


def test_scale_launcher_rejects_unsafe_run_id() -> None:
    with pytest.raises(ValueError, match="run_id"):
        prepare_summary_path("../outside", "LOW")


def test_scale_compose_allows_benchmark_only_postgres_pool_budget_overrides() -> None:
    compose = (ROOT / "docker-compose.scale.yml").read_text(encoding="utf-8")

    assert "DB_POOL_SIZE: ${BENCHMARK_DB_POOL_SIZE:-20}" in compose
    assert "DB_MAX_OVERFLOW: ${BENCHMARK_DB_MAX_OVERFLOW:-30}" in compose
    assert "DB_POOL_TIMEOUT_SECONDS: ${BENCHMARK_DB_POOL_TIMEOUT_SECONDS:-30}" in compose
    assert "BENCHMARK_CACHE_INVALIDATION_MODE: ${BENCHMARK_CACHE_INVALIDATION_MODE:-legacy-scan}" in compose
    assert "BENCHMARK_CACHE_NAMESPACE: ${BENCHMARK_CACHE_NAMESPACE:-v1}" in compose
    assert "BENCHMARK_REDIS_TCP_PREFLIGHT: ${BENCHMARK_REDIS_TCP_PREFLIGHT:-enabled}" in compose


def test_scale_harness_requires_generation_cache_isolation(monkeypatch: pytest.MonkeyPatch) -> None:
    _safe_env(monkeypatch)
    monkeypatch.setattr(scale_harness, "get_settings", lambda: _Settings("benchmark"))
    monkeypatch.setenv("BENCHMARK_CACHE_NAMESPACE", "v1")

    with pytest.raises(RuntimeError, match="v2 cache namespace"):
        scale_harness.require_safe_environment()


def test_scale_compose_labels_postgres_connections_by_process_role() -> None:
    compose = (ROOT / "docker-compose.scale.yml").read_text(encoding="utf-8")

    for role in ("api", "celery-scale", "celery-background", "celery-beat"):
        assert f"PGAPPNAME: mosb-role-{role}" in compose
        assert f"MEMORYOS_PROCESS_ROLE: {role}" in compose


def test_scale_compose_disables_container_local_celery_pidfiles() -> None:
    compose = (ROOT / "docker-compose.scale.yml").read_text(encoding="utf-8")

    assert compose.count('"--pidfile="') == 3
    assert "/tmp/celery-background.pid" not in compose


def test_scale_launcher_requires_every_disposable_service(monkeypatch: pytest.MonkeyPatch) -> None:
    rows = [
        {"Service": service, "State": "running", "Health": "healthy" if service in {"api", "postgres", "redis", "qdrant"} else ""}
        for service in ("api", "celery-scale", "celery-background", "celery-beat", "postgres", "redis", "qdrant")
    ]
    monkeypatch.setattr(
        "scripts.run_scale_benchmark.subprocess.run",
        lambda *_args, **_kwargs: SimpleNamespace(returncode=0, stdout=json.dumps(rows)),
    )

    require_disposable_stack_ready({"MEMORYOS_SCALE_DEDICATED": "1"})


def test_scale_launcher_rejects_missing_background_worker(monkeypatch: pytest.MonkeyPatch) -> None:
    rows = [
        {"Service": service, "State": "running", "Health": "healthy" if service in {"api", "postgres", "redis", "qdrant"} else ""}
        for service in ("api", "celery-scale", "celery-beat", "postgres", "redis", "qdrant")
    ]
    monkeypatch.setattr(
        "scripts.run_scale_benchmark.subprocess.run",
        lambda *_args, **_kwargs: SimpleNamespace(returncode=0, stdout=json.dumps(rows)),
    )

    with pytest.raises(RuntimeError, match="celery-background"):
        require_disposable_stack_ready({"MEMORYOS_SCALE_DEDICATED": "1"})


def test_runtime_telemetry_is_disabled_without_all_benchmark_guards(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MEMORYOS_SCALE_DEDICATED", raising=False)
    monkeypatch.setenv("BENCHMARK_RUNTIME_TELEMETRY", "1")
    assert runtime_telemetry_enabled(app_env="benchmark") is False

    monkeypatch.setenv("MEMORYOS_SCALE_DEDICATED", "1")
    assert runtime_telemetry_enabled(app_env="production") is False


def test_runtime_telemetry_requires_explicit_benchmark_opt_in(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MEMORYOS_SCALE_DEDICATED", "1")
    monkeypatch.setenv("BENCHMARK_RUNTIME_TELEMETRY", "1")
    assert runtime_telemetry_enabled(app_env="benchmark") is True


def test_scale_compose_keeps_runtime_telemetry_off_by_default() -> None:
    compose = (ROOT / "docker-compose.scale.yml").read_text(encoding="utf-8")
    assert "BENCHMARK_RUNTIME_TELEMETRY: ${BENCHMARK_RUNTIME_TELEMETRY:-0}" in compose


def test_runtime_correlation_matches_slow_route_to_nearest_sample() -> None:
    events = parse_events(
        [
            '2026-08-21T10:00:00Z {"event":"benchmark_runtime_sample","event_loop_lag_ms_max":4}',
            '2026-08-21T10:00:05Z {"event":"benchmark_runtime_sample","event_loop_lag_ms_max":4900,"gc_pause_ms_max":2}',
            '2026-08-21T10:00:04.9Z {"event":"memory_retrieve_benchmark_phases","route_ms":5000,"context_ms":4900,"retrieval_ms":10}',
        ]
    )
    result = summarize(
        events,
        host_heartbeat={"anomalies": [{"timestamp_unix_ms": 1787306404950, "lag_ms": 4800}]},
    )
    assert result["slow_retrieval_route_count"] == 1
    assert result["slow_route_correlations"][0]["event_loop_lag_ms_max"] == 4900
    assert result["slow_route_correlations"][0]["nearest_sample_distance_ms"] == pytest.approx(100)
    assert result["slow_route_correlations"][0]["nearest_host_heartbeat_lag_ms"] == 4800


def test_host_heartbeat_writes_machine_readable_summary(tmp_path: Path) -> None:
    heartbeat = HostHeartbeat(interval_seconds=0.001, anomaly_seconds=1)
    heartbeat.start()
    heartbeat._stop.wait(0.01)
    heartbeat.stop()
    output = tmp_path / "host-heartbeat.json"
    heartbeat.write(output)
    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["samples"] > 0
    assert payload["sample_interval_ms"] == 1


def test_distribution_is_deterministic_and_handles_empty_input() -> None:
    assert scale_harness._distribution([]) == {
        "count": 0,
        "p50": 0.0,
        "p95": 0.0,
        "p99": 0.0,
        "max": 0.0,
    }
    assert scale_harness._distribution([10, 20, 30, 40, 50]) == {
        "count": 5,
        "p50": 30,
        "p95": 40,
        "p99": 40,
        "max": 50,
    }
