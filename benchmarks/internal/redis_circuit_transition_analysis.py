from __future__ import annotations

import argparse
import json
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any


def _payload(line: str) -> dict[str, Any] | None:
    start, end = line.find("{"), line.rfind("}")
    if start < 0 or end < start:
        return None
    try:
        value = json.loads(line[start : end + 1])
    except json.JSONDecodeError:
        return None
    return value if isinstance(value, dict) else None


def _timestamp(payload: dict[str, Any], line: str) -> float:
    captured = payload.get("captured_at_unix_ms")
    if isinstance(captured, (int, float)):
        return float(captured)
    token = line.split(" ", 1)[0]
    try:
        return datetime.fromisoformat(token.replace("Z", "+00:00")).timestamp() * 1000
    except ValueError:
        return 0.0


def analyze(raw_log: Path) -> dict[str, Any]:
    events: list[dict[str, Any]] = []
    for line in raw_log.read_text(encoding="utf-8", errors="replace").splitlines():
        payload = _payload(line)
        if payload is None:
            continue
        payload = dict(payload)
        payload["_timestamp_ms"] = _timestamp(payload, line)
        events.append(payload)
    events.sort(key=lambda row: row["_timestamp_ms"])

    redis = [row for row in events if row.get("event") == "redis_benchmark_timing"]
    auth = [row for row in events if row.get("event") == "api_key_auth_benchmark"]
    transitions = [row for row in redis if row.get("metric") == "circuit_transition"]
    gates = [row for row in redis if row.get("metric") == "circuit_gate" and row.get("outcome") == "open"]
    errors = [row for row in redis if row.get("outcome") == "error"]
    opens = [row for row in transitions if row.get("new_state") == "OPEN"]
    first_open = opens[0] if opens else None
    threshold_opens = [
        row for row in opens
        if row.get("transition") == "failure_recorded"
        and int(row.get("failure_count", 0) or 0) >= 5
    ]
    first_time = float(first_open["_timestamp_ms"]) if first_open else 0.0
    sequence = [
        row for row in redis
        if first_open and first_time - 5000 <= float(row["_timestamp_ms"]) <= first_time + 5000
    ]

    def counts(field: str, rows: list[dict[str, Any]]) -> dict[str, int]:
        return dict(Counter(str(row.get(field, "unknown")) for row in rows))

    command_errors = [row for row in errors if row.get("metric") == "command"]
    execution_errors = [row for row in errors if row.get("metric") == "circuit_execution"]
    pool_errors = [row for row in errors if row.get("metric") == "pool_acquisition"]
    connection_errors = [row for row in errors if row.get("metric") == "connection"]
    return {
        "schema_version": "1.0",
        "holdout_used": False,
        "event_counts": {
            "redis_events": len(redis),
            "transitions": len(transitions),
            "open_transitions": len(opens),
            "circuit_open_gates": len(gates),
            "command_errors": len(command_errors),
            "execution_errors": len(execution_errors),
            "pool_errors": len(pool_errors),
            "connection_errors": len(connection_errors),
            "auth_events": len(auth),
        },
        "transition_types": counts("transition", transitions),
        "open_transition_types": counts("transition", opens),
        "open_transition_sources": counts("source", opens),
        "transition_sources": counts("source", transitions),
        "transition_reasons": counts("reason", transitions),
        "transition_operations": counts("operation", transitions),
        "transition_client_roles": counts("client_role", transitions),
        "command_errors_by_command": counts("command", command_errors),
        "command_errors_by_reason": counts("reason", command_errors),
        "execution_errors_by_reason": counts("reason", execution_errors),
        "pool_errors_by_reason": counts("reason", pool_errors),
        "connection_errors_by_reason": counts("reason", connection_errors),
        "auth_cache_outcomes": counts(
            "outcome", [row for row in auth if row.get("phase") == "cache_lookup"]
        ),
        "auth_database_outcomes": counts(
            "outcome", [row for row in auth if row.get("phase") == "database_fallback"]
        ),
        "first_open": first_open,
        "first_threshold_open": threshold_opens[0] if threshold_opens else None,
        "first_open_sequence_5s": sequence,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--raw-log", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    payload = analyze(args.raw_log)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(payload, indent=2))


if __name__ == "__main__":
    main()
