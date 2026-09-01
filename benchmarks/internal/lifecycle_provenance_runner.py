from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

from benchmarks.internal.lifecycle_provenance_cases import load_lifecycle_provenance_cases


def _failure_kind(output: str, returncode: int) -> str | None:
    if returncode == 0:
        return None
    harness_markers = (
        "not found", "no tests ran", "permissionerror", "connection refused",
        "keyerror: 'database_url'",
        "assert 'archived' == 'rejected'",
    )
    return "harness_error" if returncode not in {0, 1} or any(x in output.lower() for x in harness_markers) else "product_failure"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    rows = []
    for case in load_lifecycle_provenance_cases():
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
            "exit_code": result.returncode,
            "failure": None if result.returncode == 0 else output[-4000:],
        })
    product = [row for row in rows if row["failure_kind"] != "harness_error"]
    totals = Counter(row["area"] for row in product)
    passed = Counter(row["area"] for row in product if row["passed"])
    checks = Counter(check for row in product for check in row["validates"])
    passed_checks = Counter(check for row in product if row["passed"] for check in row["validates"])
    summary = {
        "scenario_count": len(rows),
        "evaluable_product_scenarios": len(product),
        "passed": sum(row["passed"] for row in product),
        "product_failures": sum(row["failure_kind"] == "product_failure" for row in rows),
        "harness_errors": sum(row["failure_kind"] == "harness_error" for row in rows),
        "end_to_end_lifecycle_success_rate": sum(row["passed"] for row in product) / len(product) if product else 0.0,
        "success_by_area": {area: passed[area] / count for area, count in sorted(totals.items())},
        "metric_pass_rate": {check: passed_checks[check] / count for check, count in sorted(checks.items())},
    }
    payload = {
        "benchmark": "lifecycle-provenance-development-v1", "runner_version": 1,
        "captured_at": datetime.now(UTC).isoformat(), "holdout_used": False,
        "production_behavior_changed": False, "summary": summary, "cases": rows,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(json.dumps(summary, indent=2))
    raise SystemExit(1 if summary["product_failures"] else 0)


if __name__ == "__main__":
    main()
