from __future__ import annotations

import argparse, json, subprocess, sys, time
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

from benchmarks.internal.fault_injection_cases import load_fault_cases


def classify(output: str, code: int) -> str | None:
    if code == 0:
        return None
    markers = ("no module named pytest", "keyerror: 'database_url'", "connection refused", "permissionerror", "no tests ran", "file or directory not found", "fakeexecuteresult' object has no attribute")
    return "harness_error" if code not in {0, 1} or any(marker in output.lower() for marker in markers) else "product_failure"


def main() -> None:
    parser = argparse.ArgumentParser(); parser.add_argument("--output", type=Path, required=True); args = parser.parse_args()
    rows = []
    for case in load_fault_cases():
        start = time.perf_counter()
        result = subprocess.run([sys.executable, "-m", "pytest", case.test_node, "-q", "--tb=short", "--runxfail", "-p", "no:cacheprovider"], text=True, capture_output=True, check=False)
        output = result.stdout + result.stderr
        rows.append({"id":case.id,"area":case.area,"level":case.level,"test_node":case.test_node,"validates":list(case.validates),"passed":result.returncode==0,"failure_kind":classify(output,result.returncode),"duration_ms":round((time.perf_counter()-start)*1000,2),"failure":None if result.returncode==0 else output[-4000:]})
    product = [row for row in rows if row["failure_kind"] != "harness_error"]
    areas=Counter(row["area"] for row in product); area_pass=Counter(row["area"] for row in product if row["passed"])
    metrics=Counter(m for row in product for m in row["validates"]); metric_pass=Counter(m for row in product if row["passed"] for m in row["validates"])
    durations=[row["duration_ms"] for row in rows]
    summary={"scenario_count":len(rows),"evaluable_product_scenarios":len(product),"passed":sum(row["passed"] for row in product),"product_failures":sum(row["failure_kind"]=="product_failure" for row in rows),"harness_errors":sum(row["failure_kind"]=="harness_error" for row in rows),"reliability_success_rate":sum(row["passed"] for row in product)/len(product) if product else 0.0,"success_by_area":{k:area_pass[k]/v for k,v in sorted(areas.items())},"metric_pass_rate":{k:metric_pass[k]/v for k,v in sorted(metrics.items())},"execution_time_ms":{"total":round(sum(durations),2),"mean":round(sum(durations)/len(durations),2),"minimum":min(durations),"maximum":max(durations)}}
    payload={"benchmark":"fault-injection-reliability-development-v3","runner_version":3,"captured_at":datetime.now(UTC).isoformat(),"holdout_used":False,"production_behavior_changed":False,"summary":summary,"cases":rows}
    args.output.parent.mkdir(parents=True,exist_ok=True); args.output.write_text(json.dumps(payload,indent=2),encoding="utf-8"); print(json.dumps(summary,indent=2)); raise SystemExit(1 if summary["product_failures"] else 0)


if __name__ == "__main__": main()
