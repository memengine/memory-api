from __future__ import annotations

import argparse
import json
import math
import subprocess
from collections import Counter
from pathlib import Path
from typing import Any


def _percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, math.ceil(len(ordered) * fraction) - 1)
    return round(ordered[index], 3)


def _distribution(values: list[float]) -> dict[str, float | int | None]:
    return {
        "count": len(values),
        "p50": _percentile(values, 0.50),
        "p95": _percentile(values, 0.95),
        "p99": _percentile(values, 0.99),
        "max": round(max(values), 3) if values else None,
    }


def _events_from_logs() -> list[dict[str, Any]]:
    completed = subprocess.run(
        [
            "docker", "compose", "-p", "memoryos-scale", "-f", "docker-compose.scale.yml",
            "--env-file", ".env.scale", "logs", "--no-color", "api",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError("Unable to read disposable API logs.")
    events: list[dict[str, Any]] = []
    for line in completed.stdout.splitlines():
        start = line.find("{")
        if start < 0:
            continue
        try:
            events.append(json.loads(line[start:]))
        except json.JSONDecodeError:
            continue
    return events


def analyze(observer_path: Path, output_path: Path) -> dict[str, Any]:
    observer = json.loads(observer_path.read_text(encoding="utf-8"))
    events = _events_from_logs()
    long_rows = [
        row
        for sample in observer.get("samples", [])
        for row in sample.get("long_transactions", [])
    ]
    clarification_rows = [
        row for row in long_rows if "clarification_queue" in str(row.get("query_shape", "")).lower()
    ]
    blocked_rows = [row for row in long_rows if row.get("blocking_pids")]
    blocked_clarifications = [row for row in clarification_rows if row.get("blocking_pids")]

    phases = [
        event for event in events if event.get("event") == "memory_retrieve_benchmark_phases"
    ]
    clarification_phase_ms = [float(event.get("clarification_ms") or 0) for event in phases]
    route_ms = [float(event.get("route_ms") or 0) for event in phases]
    transaction_events = [
        event for event in events if event.get("event") == "postgres_benchmark_transaction"
    ]
    clarification_transactions = [
        event for event in transaction_events
        if "clarification_queue" in str(event.get("last_statement_shape", "")).lower()
    ]
    clarification_transaction_ms = [
        float(event.get("latency_ms") or 0) for event in clarification_transactions
    ]
    clarification_sql = [
        event for event in events
        if event.get("event") == "postgres_benchmark_sql"
        and "clarification_queue" in str(event.get("statement_shape", "")).lower()
    ]
    clarification_sql_ms = [float(event.get("latency_ms") or 0) for event in clarification_sql]
    auth_events = [event for event in events if event.get("event") == "api_key_auth_benchmark"]
    auth_by_phase: dict[str, list[dict[str, Any]]] = {}
    for event in auth_events:
        auth_by_phase.setdefault(str(event.get("phase", "unknown")), []).append(event)
    api_key_update_transactions = [
        event for event in transaction_events
        if "update api_keys set last_used_at" in str(event.get("last_statement_shape", "")).lower()
    ]
    redis_events = [event for event in events if event.get("event") == "redis_benchmark_timing"]

    wait_events = Counter(
        f"{row.get('wait_event_type') or 'none'}:{row.get('wait_event') or 'none'}"
        for row in clarification_rows
    )
    blocker_edges = Counter(
        (int(row["pid"]), int(blocker))
        for row in blocked_rows
        for blocker in row.get("blocking_pids", [])
    )
    payload = {
        "schema_version": "1.0",
        "holdout_used": False,
        "observer": {
            "samples": observer.get("sample_count"),
            "failures": observer.get("failure_count"),
            "first_total": observer.get("first_total"),
            "last_total": observer.get("last_total"),
            "max_total": observer.get("max_total"),
            "long_transaction_observations": len(long_rows),
            "blocked_transaction_observations": len(blocked_rows),
            "distinct_blocked_pids": len({int(row["pid"]) for row in blocked_rows}),
            "distinct_blocker_pids": len({
                int(blocker) for row in blocked_rows for blocker in row.get("blocking_pids", [])
            }),
            "blocker_edges": [
                {"blocked_pid": edge[0], "blocker_pid": edge[1], "observations": count}
                for edge, count in blocker_edges.most_common(25)
            ],
            "blocked_query_shapes": [
                {
                    "query_shape": key[0],
                    "wait_event_type": key[1],
                    "wait_event": key[2],
                    "observations": count,
                }
                for key, count in Counter(
                    (
                        str(row.get("query_shape", "unknown")),
                        str(row.get("wait_event_type") or "none"),
                        str(row.get("wait_event") or "none"),
                    )
                    for row in blocked_rows
                ).most_common(25)
            ],
        },
        "clarification_observer": {
            "observations": len(clarification_rows),
            "blocked_observations": len(blocked_clarifications),
            "distinct_pids": len({int(row["pid"]) for row in clarification_rows}),
            "transaction_age_seconds": _distribution([
                float(row.get("transaction_age_seconds") or 0) for row in clarification_rows
            ]),
            "query_age_seconds": _distribution([
                float(row.get("query_age_seconds") or 0) for row in clarification_rows
            ]),
            "wait_events": dict(wait_events.most_common()),
        },
        "application_events": {
            "retrieval_phase_count": len(phases),
            "clarification_phase_ms": _distribution(clarification_phase_ms),
            "route_ms": _distribution(route_ms),
            "clarification_transaction_count": len(clarification_transactions),
            "clarification_transaction_ms": _distribution(clarification_transaction_ms),
            "clarification_transaction_over_2s": sum(value >= 2000 for value in clarification_transaction_ms),
            "clarification_transaction_over_5s": sum(value >= 5000 for value in clarification_transaction_ms),
            "clarification_sql_count": len(clarification_sql),
            "clarification_sql_ms": _distribution(clarification_sql_ms),
            "clarification_sql_errors": sum(event.get("outcome") == "error" for event in clarification_sql),
            "api_key_update_transaction_ms": _distribution([
                float(event.get("latency_ms") or 0) for event in api_key_update_transactions
            ]),
            "auth_phases": {
                phase: {
                    "duration_ms": _distribution([
                        float(event.get("duration_ms") or 0) for event in phase_events
                    ]),
                    "outcomes": dict(Counter(str(event.get("outcome", "unknown")) for event in phase_events)),
                }
                for phase, phase_events in sorted(auth_by_phase.items())
            },
            "redis": {
                "circuit_open_fallbacks": sum(
                    event.get("metric") == "circuit_gate" and event.get("outcome") == "open"
                    for event in redis_events
                ),
                "command_errors": sum(
                    event.get("metric") == "command" and event.get("outcome") == "error"
                    for event in redis_events
                ),
            },
        },
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return payload


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--observer", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(analyze(args.observer, args.output), indent=2))


if __name__ == "__main__":
    main()
