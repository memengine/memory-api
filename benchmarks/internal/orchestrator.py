from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_MANIFEST = ROOT / "benchmarks" / "internal" / "benchmark-manifest-v1.json"
HARNESS_MARKERS = (
    "keyerror: 'database_url'",
    "connection refused",
    "network is unreachable",
    "permissionerror",
    "no module named",
    "file or directory not found",
    "could not connect",
)


def _value(payload: dict[str, Any], path: str) -> Any:
    value: Any = payload
    for part in path.split("."):
        if not isinstance(value, dict) or part not in value:
            raise KeyError(path)
        value = value[part]
    return value


def _evaluate(payload: dict[str, Any], acceptance: dict[str, Any]) -> list[str]:
    failures: list[str] = []
    for path, rule in acceptance.items():
        if path == "exit_code":
            continue
        try:
            actual = _value(payload, path)
        except KeyError:
            failures.append(f"missing metric: {path}")
            continue
        if "min" in rule and actual < rule["min"]:
            failures.append(f"{path}={actual} below {rule['min']}")
        if "max" in rule and actual > rule["max"]:
            failures.append(f"{path}={actual} above {rule['max']}")
        if "equals" in rule and actual != rule["equals"]:
            failures.append(f"{path}={actual!r} expected {rule['equals']!r}")
    return failures


def _numeric_deltas(current: Any, baseline: Any, prefix: str = "") -> dict[str, float]:
    deltas: dict[str, float] = {}
    if isinstance(current, dict) and isinstance(baseline, dict):
        for key in current.keys() & baseline.keys():
            path = f"{prefix}.{key}" if prefix else key
            deltas.update(_numeric_deltas(current[key], baseline[key], path))
    elif isinstance(current, (int, float)) and not isinstance(current, bool) and isinstance(baseline, (int, float)) and not isinstance(baseline, bool):
        deltas[prefix] = round(float(current) - float(baseline), 8)
    return deltas


def _load_json(path: Path | None) -> dict[str, Any]:
    if path is None or not path.exists() or path.suffix.lower() != ".json":
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _docker_available() -> bool:
    result = subprocess.run(
        ["docker", "compose", "ps", "-q", "api"], cwd=ROOT,
        text=True, capture_output=True, check=False,
    )
    return result.returncode == 0 and bool(result.stdout.strip())


def _run_suite(suite: dict[str, Any], run_dir: Path, *, use_docker: bool) -> dict[str, Any]:
    started = time.perf_counter()
    output = run_dir / f"{suite['name']}.json"
    output_argument = str(output)
    if use_docker and suite["tier"] == "integration" and "DATABASE_URL" not in os.environ:
        output_argument = str(output.relative_to(ROOT)).replace("\\", "/")
    command = [part.format(python=sys.executable, output=output_argument) for part in suite["runner"]]
    execution = "host"
    if use_docker and suite["tier"] == "integration" and "DATABASE_URL" not in os.environ:
        container_command = ["python" if index == 0 else part for index, part in enumerate(command)]
        command = ["docker", "compose", "exec", "-T", "api", *container_command]
        execution = "docker-compose-api"
    result = subprocess.run(command, cwd=ROOT, text=True, capture_output=True, check=False)
    combined = result.stdout + result.stderr
    result_payload = _load_json(output if output.exists() else None)
    gate_failures = _evaluate(result_payload, suite.get("acceptance", {}))
    expected_exit = suite.get("acceptance", {}).get("exit_code", 0)
    if result.returncode != expected_exit:
        gate_failures.append(f"exit_code={result.returncode} expected {expected_exit}")
    failure_kind = None
    if gate_failures:
        failure_kind = "harness_error" if result.returncode not in {0, 1} or any(marker in combined.lower() for marker in HARNESS_MARKERS) else "product_failure"
        if result.returncode == 0 and any(item.startswith("missing metric:") for item in gate_failures):
            failure_kind = "harness_error"
    baseline_path = ROOT / suite["baseline"] if suite.get("baseline") else None
    baseline = _load_json(baseline_path)
    return {
        "name": suite["name"], "version": suite["version"], "tier": suite["tier"],
        "status": "passed" if not gate_failures else "failed", "failure_kind": failure_kind,
        "gate_failures": gate_failures, "duration_ms": round((time.perf_counter() - started) * 1000, 2),
        "execution": execution, "result_artifact": str(output.relative_to(ROOT)) if output.exists() else None,
        "baseline": suite.get("baseline"), "baseline_deltas": _numeric_deltas(result_payload, baseline),
        "provider_calls": suite.get("provider_calls", False),
        "provider_cost_usd": float(_find_cost(result_payload)),
        "output_tail": combined[-2000:] if gate_failures else None,
    }


def _find_cost(payload: Any) -> float:
    if isinstance(payload, dict):
        total = 0.0
        for key, value in payload.items():
            if key in {"estimated_cost_usd", "estimated_provider_cost_usd", "total_cost_usd"} and isinstance(value, (int, float)):
                total += float(value)
            else:
                total += _find_cost(value)
        return total
    if isinstance(payload, list):
        return sum(_find_cost(item) for item in payload)
    return 0.0


def _write_report(path: Path, aggregate: dict[str, Any]) -> None:
    lines = [
        f"# Internal benchmark aggregate {aggregate['run_id']}", "",
        f"Tier: **{aggregate['tier']}**  ",
        f"Status: **{aggregate['status']}**  ",
        f"Runtime: **{aggregate['summary']['total_runtime_ms'] / 1000:.2f}s**  ",
        f"Provider cost: **${aggregate['summary']['provider_cost_usd']:.6f}**", "",
        "| Suite | Status | Classification | Runtime |", "|---|---:|---|---:|",
    ]
    for row in aggregate["suites"]:
        lines.append(f"| {row['name']} | {row['status']} | {row.get('failure_kind') or '-'} | {row.get('duration_ms', 0) / 1000:.2f}s |")
    if aggregate["skipped"]:
        lines.extend(["", "## Skipped"])
        lines.extend(f"- `{row['name']}`: {row['reason']}" for row in aggregate["skipped"])
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--tier", choices=["fast", "integration", "provider", "holdout"], required=True)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--suite", action="append", default=[])
    parser.add_argument("--output-dir", type=Path, default=ROOT / "artifacts" / "internal-benchmarks" / "aggregate")
    parser.add_argument("--approve-provider", action="store_true")
    parser.add_argument("--allow-holdout", action="store_true")
    args = parser.parse_args()
    manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    if args.tier == "holdout":
        holdout = manifest["holdout"]
        if not args.allow_holdout or os.getenv(holdout["approval_environment"]) != holdout["approval_token"]:
            raise SystemExit("Holdout is locked: explicit flag and approval environment token are both required.")
        raise SystemExit("No routine holdout runner is registered; use a separately reviewed manual command.")
    if args.allow_holdout:
        raise SystemExit("--allow-holdout is valid only with --tier holdout")
    if args.tier == "provider" and not args.approve_provider:
        raise SystemExit("Provider tier requires --approve-provider; it may incur paid calls.")

    run_id = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ") + f"-{args.tier}-v1"
    run_dir = args.output_dir / run_id
    run_dir.mkdir(parents=True, exist_ok=False)
    selected = [item for item in manifest["suites"] if item["tier"] == args.tier and (not args.suite or item["name"] in args.suite)]
    rows: list[dict[str, Any]] = []
    skipped: list[dict[str, str]] = []
    docker_available: bool | None = None
    for suite in selected:
        if suite.get("runner") is None:
            skipped.append({"name": suite["name"], "reason": "no executable runner registered"})
            continue
        required_any = suite.get("required_env_any", [])
        if required_any and not any(os.getenv(name) for name in required_any):
            skipped.append({"name": suite["name"], "reason": f"missing one of: {', '.join(required_any)}"})
            continue
        if args.tier == "integration" and "DATABASE_URL" not in os.environ:
            if docker_available is None:
                docker_available = _docker_available()
            if not docker_available:
                skipped.append({"name": suite["name"], "reason": "DATABASE_URL absent and local Compose api unavailable"})
                continue
        rows.append(_run_suite(suite, run_dir, use_docker=bool(docker_available)))
    product_failures = sum(row["failure_kind"] == "product_failure" for row in rows)
    harness_errors = sum(row["failure_kind"] == "harness_error" for row in rows)
    aggregate = {
        "schema_version": "1.0", "manifest_version": manifest["manifest_version"], "run_id": run_id,
        "captured_at": datetime.now(UTC).isoformat(), "tier": args.tier, "holdout_used": False,
        "status": "passed" if product_failures == 0 and harness_errors == 0 else "failed",
        "summary": {"registered": len(selected), "executed": len(rows), "passed": sum(row["status"] == "passed" for row in rows), "product_failures": product_failures, "harness_errors": harness_errors, "skipped": len(skipped), "total_runtime_ms": round(sum(row["duration_ms"] for row in rows), 2), "provider_cost_usd": round(sum(row["provider_cost_usd"] for row in rows), 8)},
        "suites": rows, "skipped": skipped,
    }
    json_path = run_dir / "aggregate.json"; report_path = run_dir / "aggregate.md"
    json_path.write_text(json.dumps(aggregate, indent=2), encoding="utf-8"); _write_report(report_path, aggregate)
    print(json.dumps({**aggregate["summary"], "status": aggregate["status"], "artifact": str(json_path)}, indent=2))
    raise SystemExit(1 if aggregate["status"] == "failed" else 0)


if __name__ == "__main__":
    main()
