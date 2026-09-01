from __future__ import annotations

import argparse
import json
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any


def _distribution(values: list[float]) -> dict[str, float | int]:
    if not values:
        return {"count": 0, "p50": 0.0, "p95": 0.0, "p99": 0.0, "max": 0.0}
    ordered = sorted(values)

    def percentile(fraction: float) -> float:
        index = min(len(ordered) - 1, int((len(ordered) - 1) * fraction))
        return round(ordered[index], 3)

    return {
        "count": len(ordered),
        "p50": percentile(0.50),
        "p95": percentile(0.95),
        "p99": percentile(0.99),
        "max": round(ordered[-1], 3),
    }


def _read_log(path: Path) -> str:
    raw = path.read_bytes()
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")) or b"\x00" in raw[:200]:
        return raw.decode("utf-16")
    return raw.decode("utf-8", errors="replace")


def _events(path: Path) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for line_number, line in enumerate(_read_log(path).splitlines(), 1):
        start = line.find("{")
        if start < 0:
            continue
        try:
            payload = json.loads(line[start:])
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            payload["_line"] = line_number
            events.append(payload)
    return events


def _normalized_path(path: str) -> str:
    if path.startswith("/v1/memories/jobs/"):
        return "/v1/memories/jobs/:id"
    return path


def analyze(log_path: Path, k6_path: Path) -> dict[str, Any]:
    events = _events(log_path)
    k6 = json.loads(k6_path.read_text(encoding="utf-8"))
    phase_values: dict[tuple[str, str], list[float]] = defaultdict(list)
    auth_values: dict[tuple[str, str], list[float]] = defaultdict(list)
    redis_values: dict[tuple[str, str, str], list[float]] = defaultdict(list)
    engine_owners: Counter[str] = Counter()

    for event in events:
        event_name = str(event.get("event", ""))
        if event_name == "request_phase_benchmark":
            phase_values[(_normalized_path(str(event.get("path", "unspecified"))), str(event.get("phase", "unknown")))].append(
                float(event.get("duration_ms", 0.0))
            )
        elif event_name == "api_key_auth_benchmark":
            auth_values[(str(event.get("phase", "unknown")), str(event.get("outcome", "unknown")))].append(
                float(event.get("duration_ms", 0.0))
            )
        elif event_name == "redis_benchmark_timing":
            redis_values[(
                str(event.get("metric", "unknown")),
                str(event.get("outcome", "unknown")),
                str(event.get("command", "none")),
            )].append(float(event.get("latency_ms", 0.0)))
        elif event_name == "postgres_benchmark_engine_created":
            engine_owners[str(event.get("owner", "unknown"))] += 1

    log_text = _read_log(log_path)
    api_auth = [
        value
        for (path, phase), values in phase_values.items()
        if phase == "api_key_auth" and path != "/health"
        for value in values
    ]
    non_auth = [
        value
        for (path, phase), values in phase_values.items()
        if phase != "api_key_auth" and path != "/health"
        for value in values
    ]
    cache_hits = len(auth_values.get(("cache_lookup", "hit"), []))
    cache_misses = len(auth_values.get(("cache_lookup", "miss"), []))
    cache_timeouts = len(auth_values.get(("cache_lookup", "timeout"), []))
    cache_total = cache_hits + cache_misses + cache_timeouts

    return {
        "schema_version": "1.0",
        "analysis": "cache-generation-low-tail-diagnosis",
        "source": {"log": str(log_path), "k6": str(k6_path)},
        "request_phases": [
            {
                "path": path,
                "phase": phase,
                "latency_ms": _distribution(values),
                "over_750_ms": sum(value > 750 for value in values),
                "over_1500_ms": sum(value > 1500 for value in values),
            }
            for (path, phase), values in sorted(phase_values.items())
            if path != "/health"
        ],
        "aggregate_request_phase": {
            "api_key_auth_ms": _distribution(api_auth),
            "api_key_auth_over_750_ms": sum(value > 750 for value in api_auth),
            "api_key_auth_over_1500_ms": sum(value > 1500 for value in api_auth),
            "other_instrumented_phase_ms": _distribution(non_auth),
            "other_phase_over_750_ms": sum(value > 750 for value in non_auth),
        },
        "authentication": {
            "cache_hits": cache_hits,
            "cache_misses": cache_misses,
            "cache_timeouts": cache_timeouts,
            "cache_hit_rate": round(cache_hits / cache_total, 6) if cache_total else 0.0,
            "by_phase_outcome": [
                {"phase": phase, "outcome": outcome, "latency_ms": _distribution(values)}
                for (phase, outcome), values in sorted(auth_values.items())
            ],
        },
        "redis": {
            "request_path_scan_count": log_text.count('"command": "SCAN"'),
            "circuit_open_fallback_count": log_text.count("Redis circuit open"),
            "timings": [
                {
                    "metric": metric,
                    "outcome": outcome,
                    "command": command,
                    "latency_ms": _distribution(values),
                }
                for (metric, outcome, command), values in sorted(redis_values.items())
            ],
        },
        "postgres_engine_creation": {
            "total": sum(engine_owners.values()),
            "by_owner": dict(engine_owners.most_common()),
        },
        "k6_tail": {
            name: k6.get("metrics", {}).get(name, {}).get("values", {})
            for name in ("add_ack_ms", "retrieve_ms", "job_completion_ms", "http_req_failed", "api_errors")
        },
        "diagnosis": {
            "primary_chain": [
                "tcp_preflight_failures",
                "shared_redis_circuit_open",
                "authentication_cache_fallback",
                "database_and_bcrypt_authentication",
                "api_tail_latency",
            ],
            "generation_invalidation_is_primary_tail_cause": False,
            "route_core_localization": "insufficient per-request correlation in captured artifact",
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--log", type=Path, required=True)
    parser.add_argument("--k6", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = analyze(args.log, args.k6)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
