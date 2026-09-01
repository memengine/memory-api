from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

from benchmarks.internal.governance_integrity_cases import load_governance_integrity_cases


def _failure_kind(output: str, returncode: int) -> str | None:
    if returncode == 0:
        return None
    markers = (
        "keyerror: 'database_url'", "connection refused", "permissionerror",
        "no tests ran", "file or directory not found", "redis",
    )
    return "harness_error" if returncode not in {0, 1} or any(x in output.lower() for x in markers) else "product_failure"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    rows = []
    for case in load_governance_integrity_cases():
        started = time.perf_counter()
        result = subprocess.run(
            [sys.executable, "-m", "pytest", case.test_node, "-q", "--tb=short", "--runxfail", "-p", "no:cacheprovider"],
            text=True, capture_output=True, check=False,
        )
        output = result.stdout + result.stderr
        rows.append({
            "id": case.id, "area": case.area, "level": case.level,
            "test_node": case.test_node, "validates": list(case.validates),
            "passed": result.returncode == 0,
            "failure_kind": _failure_kind(output, result.returncode),
            "duration_ms": round((time.perf_counter() - started) * 1000, 2),
            "failure": None if result.returncode == 0 else output[-4000:],
        })
    product = [row for row in rows if row["failure_kind"] != "harness_error"]
    area_total = Counter(row["area"] for row in product)
    area_pass = Counter(row["area"] for row in product if row["passed"])
    metric_total = Counter(metric for row in product for metric in row["validates"])
    metric_pass = Counter(metric for row in product if row["passed"] for metric in row["validates"])
    durations = [float(row["duration_ms"]) for row in rows]
    summary = {
        "scenario_count": len(rows),
        "evaluable_product_scenarios": len(product),
        "passed": sum(row["passed"] for row in product),
        "product_failures": sum(row["failure_kind"] == "product_failure" for row in rows),
        "harness_errors": sum(row["failure_kind"] == "harness_error" for row in rows),
        "governance_success_rate": sum(row["passed"] for row in product) / len(product) if product else 0.0,
        "success_by_area": {key: area_pass[key] / value for key, value in sorted(area_total.items())},
        "metric_pass_rate": {key: metric_pass[key] / value for key, value in sorted(metric_total.items())},
        "execution_time_ms": {
            "total": round(sum(durations), 2),
            "mean": round(sum(durations) / len(durations), 2) if durations else 0.0,
            "minimum": min(durations) if durations else 0.0,
            "maximum": max(durations) if durations else 0.0,
        },
    }
    payload = {
        "benchmark": "governance-integrity-development-v2", "runner_version": 2,
        "captured_at": datetime.now(UTC).isoformat(), "holdout_used": False,
        "production_behavior_changed": False, "summary": summary, "cases": rows,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(json.dumps(summary, indent=2))
    raise SystemExit(1 if summary["product_failures"] else 0)


if __name__ == "__main__":
    main()
