from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

from benchmarks.internal.integration_reliability_cases import load_integration_reliability_cases


def _failure_kind(output: str, returncode: int) -> str | None:
    if returncode == 0:
        return None
    if "FakeExecuteResult' object has no attribute 'scalar_one_or_none'" in output:
        return "harness_error"
    if "no module named pytest" in output.lower():
        return "harness_error"
    return "product_failure" if returncode == 1 else "harness_error"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    rows = []
    for case in load_integration_reliability_cases():
        started = time.perf_counter()
        result = subprocess.run(
            [sys.executable, "-m", "pytest", case.test_node, "-q", "--tb=short", "--runxfail", "-p", "no:cacheprovider"],
            text=True, capture_output=True, check=False,
        )
        output = result.stdout + result.stderr
        kind = _failure_kind(output, result.returncode)
        rows.append({
            "id": case.id, "area": case.area, "level": case.level,
            "test_node": case.test_node, "validates": list(case.validates),
            "passed": result.returncode == 0, "failure_kind": kind,
            "duration_ms": round((time.perf_counter() - started) * 1000, 2),
            "exit_code": result.returncode,
            "failure": None if result.returncode == 0 else output[-4000:],
        })
    product_rows = [row for row in rows if row["failure_kind"] != "harness_error"]
    totals = Counter(row["area"] for row in product_rows)
    passed = Counter(row["area"] for row in product_rows if row["passed"])
    summary = {
        "scenario_count": len(rows), "evaluable_product_scenarios": len(product_rows),
        "passed": sum(row["passed"] for row in product_rows),
        "product_failures": sum(row["failure_kind"] == "product_failure" for row in rows),
        "harness_errors": sum(row["failure_kind"] == "harness_error" for row in rows),
        "integration_success_rate": sum(row["passed"] for row in product_rows) / len(product_rows),
        "success_by_area": {area: passed[area] / count for area, count in sorted(totals.items())},
    }
    payload = {
        "benchmark": "integration-reliability-development-v1", "runner_version": 2,
        "captured_at": datetime.now(UTC).isoformat(), "holdout_used": False,
        "production_behavior_changed": False, "summary": summary, "cases": rows,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(json.dumps(summary, indent=2))
    raise SystemExit(0 if summary["product_failures"] == 0 else 1)


if __name__ == "__main__":
    main()
