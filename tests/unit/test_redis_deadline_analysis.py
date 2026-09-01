from __future__ import annotations

import json

from benchmarks.internal.redis_deadline_analysis import analyze_log


def test_redis_deadline_analysis_separates_latency_and_failure_types(tmp_path) -> None:
    log = tmp_path / "service.log"
    rows = [
        {"event": "redis_benchmark_timing", "metric": "command", "latency_ms": 2, "outcome": "ok"},
        {"event": "redis_benchmark_timing", "metric": "command", "latency_ms": 8, "outcome": "error", "reason": "TimeoutError"},
        {"event": "redis_benchmark_timing", "metric": "tcp_preflight", "latency_ms": 1, "outcome": "ok"},
    ]
    log.write_text("\n".join(f"api-1 | {json.dumps(row)}" for row in rows), encoding="utf-8")

    result = analyze_log(log)

    assert result["latency_ms"]["command"]["count"] == 2
    assert result["failures"] == {"command:TimeoutError": 1}
    assert result["latency_ms"]["tcp_preflight"]["p99"] == 1
