from __future__ import annotations

import argparse
import json
import math
import subprocess
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any


def percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return round(ordered[min(len(ordered) - 1, math.ceil(len(ordered) * fraction) - 1)], 3)


def events_from_logs() -> list[dict[str, Any]]:
    completed = subprocess.run(
        ["docker", "compose", "-p", "memoryos-scale", "-f", "docker-compose.scale.yml",
         "--env-file", ".env.scale", "logs", "--no-color", "api", "celery-scale",
         "celery-background", "celery-beat"],
        capture_output=True, text=True, check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError("Unable to read disposable stack logs.")
    events: list[dict[str, Any]] = []
    for line in completed.stdout.splitlines():
        service = line.split(" | ", 1)[0].strip()
        start = line.find("{")
        if start < 0:
            continue
        try:
            payload = json.loads(line[start:])
        except json.JSONDecodeError:
            continue
        payload["compose_service"] = service
        events.append(payload)
    return events


def analyze(observer_path: Path, output: Path) -> dict[str, Any]:
    observer = json.loads(observer_path.read_text(encoding="utf-8"))
    events = events_from_logs()
    transactions = [event for event in events if event.get("event") == "postgres_benchmark_transaction"]
    sql_events = [event for event in events if event.get("event") == "postgres_benchmark_sql"]

    by_boundary: dict[tuple[str, str, str], list[float]] = defaultdict(list)
    for event in transactions:
        key = (
            str(event.get("compose_service", "unknown")),
            str(event.get("last_sql_callsite", "unknown")),
            str(event.get("last_statement_shape", "unknown")),
        )
        by_boundary[key].append(float(event.get("latency_ms") or 0))
    transaction_boundaries = sorted(({
        "service": key[0], "callsite": key[1], "statement_shape": key[2],
        "count": len(values), "over_2s": sum(value >= 2000 for value in values),
        "over_5s": sum(value >= 5000 for value in values),
        "p50_ms": percentile(values, .5), "p95_ms": percentile(values, .95),
        "p99_ms": percentile(values, .99), "max_ms": round(max(values), 3),
    } for key, values in by_boundary.items()), key=lambda row: (row["over_5s"], row["max_ms"]), reverse=True)

    long_rows = [row for sample in observer.get("samples", []) for row in sample.get("long_transactions", [])]
    observer_groups: dict[tuple[str, str, str], list[float]] = defaultdict(list)
    for row in long_rows:
        observer_groups[(str(row["application_name"]), str(row["state"]), str(row["query_shape"]))].append(
            float(row["transaction_age_seconds"])
        )
    long_transaction_groups = sorted(({
        "application_name": key[0], "state": key[1], "query_shape": key[2],
        "observations": len(values), "p95_age_seconds": percentile(values, .95),
        "max_age_seconds": round(max(values), 3),
    } for key, values in observer_groups.items()), key=lambda row: (row["observations"], row["max_age_seconds"]), reverse=True)

    sql_callsites = Counter(
        (str(event.get("compose_service", "unknown")), str(event.get("callsite", "unknown")), str(event.get("operation", "unknown")))
        for event in sql_events if float(event.get("latency_ms") or 0) >= 1000
    )
    payload = {
        "schema_version": "1.0",
        "holdout_used": False,
        "observer": {
            "samples": observer.get("sample_count"), "failures": observer.get("failure_count"),
            "max_connections_observed": observer.get("max_total"), "long_transaction_observations": len(long_rows),
        },
        "transaction_events": len(transactions),
        "transaction_boundaries": transaction_boundaries[:50],
        "long_transaction_groups": long_transaction_groups[:50],
        "slow_sql_callsites": [
            {"service": key[0], "callsite": key[1], "operation": key[2], "count": count}
            for key, count in sql_callsites.most_common(50)
        ],
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return payload


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--observer", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = analyze(args.observer, args.output)
    print(json.dumps({
        "observer": result["observer"],
        "transaction_events": result["transaction_events"],
        "top_boundaries": result["transaction_boundaries"][:5],
        "top_observer_groups": result["long_transaction_groups"][:5],
    }, indent=2))


if __name__ == "__main__":
    main()
