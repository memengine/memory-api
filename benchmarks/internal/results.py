from __future__ import annotations

import hashlib
import json
import os
import platform
import subprocess
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from benchmarks.internal.cases import ExtractionCase
from benchmarks.internal.metrics import ExtractionMetrics, aggregate_metrics


def build_run_record(
    cases: list[ExtractionCase],
    metrics: list[ExtractionMetrics],
    *,
    config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload = [{"id": case.id, "split": case.split, "source": case.source} for case in cases]
    return {
        "schema_version": "1.0",
        "run_id": datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ"),
        "created_at": datetime.now(UTC).isoformat(),
        "git_commit": _git_value("rev-parse", "HEAD"),
        "git_dirty": bool(_git_value("status", "--porcelain")),
        "python_version": platform.python_version(),
        "dataset_hash": hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest(),
        "config": config or {},
        "summary": aggregate_metrics(metrics),
        "cases": [
            {"id": case.id, "split": case.split, "metrics": asdict(result)}
            for case, result in zip(cases, metrics, strict=True)
        ],
    }


def write_run_record(record: dict[str, Any], output_path: str | Path) -> None:
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def compare_baseline(summary: dict[str, Any], baseline: dict[str, Any]) -> list[str]:
    failures: list[str] = []
    for metric, floor in baseline.get("minimums", {}).items():
        if float(summary.get(metric, 0.0)) < float(floor):
            failures.append(f"{metric}={summary.get(metric)} below minimum {floor}")
    for metric, ceiling in baseline.get("maximums", {}).items():
        if float(summary.get(metric, 0.0)) > float(ceiling):
            failures.append(f"{metric}={summary.get(metric)} above maximum {ceiling}")
    return failures


def _git_value(*args: str) -> str:
    try:
        return subprocess.run(
            ["git", *args], capture_output=True, text=True, check=False, timeout=2,
            env={**os.environ, "GIT_OPTIONAL_LOCKS": "0"},
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""
