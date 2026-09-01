from __future__ import annotations

import argparse
import json
import subprocess
import time
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

from benchmarks.internal.integration_reliability_cases import load_integration_reliability_cases


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    rows = []
    for case in load_integration_reliability_cases():
        started = time.perf_counter()
        result = subprocess.run(
            ["python", "-m", "pytest", case.test_node, "-q", "--tb=short", "--runxfail", "-p", "no:cacheprovider"],
            text=True, capture_output=True, check=False,
        )
        rows.append({
            "id": case.id, "area": case.area, "level": case.level,
            "test_node": case.test_node, "validates": list(case.validates),
            "passed": result.returncode == 0,
            "duration_ms": round((time.perf_counter() - started) * 1000, 2),
            "exit_code": result.returncode,
            "failure": None if result.returncode == 0 else (result.stdout + result.stderr)[-4000:],
        })
    totals = Counter(row["area"] for row in rows)
    passed = Counter(row["area"] for row in rows if row["passed"])
    summary = {
        "scenario_count": len(rows), "passed": sum(row["passed"] for row in rows),
        "failed": sum(not row["passed"] for row in rows),
        "integration_success_rate": sum(row["passed"] for row in rows) / len(rows),
        "success_by_area": {area: passed[area] / count for area, count in sorted(totals.items())},
        "harness_errors": sum(row["exit_code"] not in {0, 1} for row in rows),
    }
    payload = {
        "benchmark": "integration-reliability-development-v1",
        "captured_at": datetime.now(UTC).isoformat(), "holdout_used": False,
        "production_behavior_changed": False, "summary": summary, "cases": rows,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(json.dumps(summary, indent=2))
    raise SystemExit(0 if summary["failed"] == 0 else 1)


if __name__ == "__main__":
    main()
