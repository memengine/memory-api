from __future__ import annotations

import argparse
import json
from collections import Counter
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


def analyze_log(path: Path) -> dict[str, Any]:
    values: dict[str, list[float]] = {}
    failures: Counter[str] = Counter()
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        marker = '{"event": "redis_benchmark_timing"'
        position = line.find(marker)
        if position < 0:
            continue
        try:
            row = json.loads(line[position:])
        except json.JSONDecodeError:
            continue
        metric = str(row.get("metric", "unknown"))
        values.setdefault(metric, []).append(float(row.get("latency_ms", 0.0)))
        if row.get("outcome") == "error":
            failures[f"{metric}:{row.get('reason', 'unknown')}"] += 1
    text = path.read_text(encoding="utf-8", errors="replace")
    return {
        "latency_ms": {name: _distribution(samples) for name, samples in sorted(values.items())},
        "failures": dict(sorted(failures.items())),
        "circuit_open_fallbacks": text.count("Redis circuit open"),
        "http_500_count": text.count("500 Internal Server Error"),
        "postgres_connection_errors": text.count("too many clients"),
        "enum_errors": text.count("invalid input value for enum call_quality_blocked_layer_enum"),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("log", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    payload = analyze_log(args.log)
    args.output.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(json.dumps(payload, indent=2))


if __name__ == "__main__":
    main()
