from __future__ import annotations

import argparse
from datetime import datetime
import json
from pathlib import Path
from typing import Any


def parse_events(lines: list[str]) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for line in lines:
        brace = line.find("{")
        if brace < 0:
            continue
        try:
            timestamp = datetime.fromisoformat(line[:brace].strip().replace("Z", "+00:00")).timestamp()
            payload = json.loads(line[brace:])
        except (ValueError, json.JSONDecodeError):
            continue
        payload["_log_timestamp"] = timestamp
        events.append(payload)
    return events


def summarize(events: list[dict[str, Any]], host_heartbeat: dict[str, Any] | None = None) -> dict[str, Any]:
    samples = [event for event in events if event.get("event") == "benchmark_runtime_sample"]
    routes = [event for event in events if event.get("event") == "memory_retrieve_benchmark_phases"]
    slow_routes = sorted(
        (event for event in routes if float(event.get("route_ms", 0)) >= 250),
        key=lambda event: float(event.get("route_ms", 0)),
        reverse=True,
    )

    correlated: list[dict[str, Any]] = []
    host_anomalies = (host_heartbeat or {}).get("anomalies", [])
    for route in slow_routes[:20]:
        nearest = min(samples, key=lambda sample: abs(sample["_log_timestamp"] - route["_log_timestamp"]), default=None)
        route_unix_ms = route["_log_timestamp"] * 1000
        nearest_host = min(
            host_anomalies,
            key=lambda anomaly: abs(float(anomaly["timestamp_unix_ms"]) - route_unix_ms),
            default=None,
        )
        correlated.append(
            {
                "route_ms": route.get("route_ms", 0),
                "context_ms": route.get("context_ms", 0),
                "context_process_cpu_ms": route.get("context_process_cpu_ms"),
                "context_thread_cpu_ms": route.get("context_thread_cpu_ms"),
                "retrieval_ms": route.get("retrieval_ms", 0),
                "nearest_sample_distance_ms": round(
                    abs(nearest["_log_timestamp"] - route["_log_timestamp"]) * 1000, 3
                )
                if nearest
                else None,
                "event_loop_lag_ms_max": nearest.get("event_loop_lag_ms_max") if nearest else None,
                "gc_pause_ms_max": nearest.get("gc_pause_ms_max") if nearest else None,
                "process_cpu_percent": nearest.get("process_cpu_percent") if nearest else None,
                "cgroup_nr_throttled_delta": nearest.get("cgroup_nr_throttled_delta") if nearest else None,
                "cgroup_throttled_usec_delta": nearest.get("cgroup_throttled_usec_delta") if nearest else None,
                "nearest_host_heartbeat_distance_ms": round(
                    abs(float(nearest_host["timestamp_unix_ms"]) - route_unix_ms), 3
                )
                if nearest_host
                else None,
                "nearest_host_heartbeat_lag_ms": nearest_host.get("lag_ms") if nearest_host else None,
            }
        )

    def maximum(name: str) -> float:
        return max((float(sample.get(name, 0)) for sample in samples), default=0.0)

    return {
        "runtime_sample_count": len(samples),
        "host_heartbeat": host_heartbeat or {},
        "retrieval_route_count": len(routes),
        "slow_retrieval_route_count": len(slow_routes),
        "runtime_maxima": {
            "event_loop_lag_ms": maximum("event_loop_lag_ms_max"),
            "gc_pause_ms": maximum("gc_pause_ms_max"),
            "process_cpu_percent": maximum("process_cpu_percent"),
            "process_rss_bytes": maximum("process_rss_bytes"),
            "cgroup_nr_throttled_total": sum(int(sample.get("cgroup_nr_throttled_delta", 0)) for sample in samples),
            "cgroup_throttled_usec_total": sum(
                int(sample.get("cgroup_throttled_usec_delta", 0)) for sample in samples
            ),
        },
        "retrieval_maxima_ms": {
            "route": max((float(route.get("route_ms", 0)) for route in routes), default=0.0),
            "context": max((float(route.get("context_ms", 0)) for route in routes), default=0.0),
            "retrieval": max((float(route.get("retrieval_ms", 0)) for route in routes), default=0.0),
        },
        "slow_route_correlations": correlated,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--host-heartbeat", type=Path)
    args = parser.parse_args()
    host_heartbeat = json.loads(args.host_heartbeat.read_text(encoding="utf-8")) if args.host_heartbeat else None
    summary = summarize(
        parse_events(args.input.read_text(encoding="utf-8-sig").splitlines()),
        host_heartbeat=host_heartbeat,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
