from __future__ import annotations

import json
from pathlib import Path

from benchmarks.internal.orchestrator import _evaluate, _numeric_deltas


ROOT = Path(__file__).resolve().parents[2]
MANIFEST = ROOT / "benchmarks" / "internal" / "benchmark-manifest-v1.json"


def test_manifest_registers_tiered_suites_without_routine_holdout() -> None:
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    suites = manifest["suites"]

    assert manifest["schema_version"] == "1.0"
    assert {suite["tier"] for suite in suites} == {"fast", "integration", "provider", "holdout"}
    assert len({suite["name"] for suite in suites}) == len(suites)
    for suite in suites:
        assert {"name", "version", "classification", "dataset", "runner", "services", "provider_calls", "mode", "baseline", "acceptance", "component_status"} <= suite.keys()
        if suite["tier"] in {"fast", "integration", "provider"}:
            assert suite["classification"] == "development"
            assert "holdout" not in suite["dataset"].lower()
    assert all(not suite["provider_calls"] for suite in suites if suite["tier"] == "fast")


def test_manifest_inventory_paths_exist_without_loading_dataset_contents() -> None:
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    for suite in manifest["suites"]:
        assert (ROOT / suite["dataset"]).exists()
        if suite["baseline"]:
            assert (ROOT / suite["baseline"]).exists()
    for baseline in manifest["reference_baselines"]:
        assert (ROOT / baseline).exists()


def test_acceptance_evaluator_does_not_loosen_thresholds() -> None:
    acceptance = {
        "summary.success": {"min": 1.0},
        "summary.failures": {"max": 0},
        "holdout_used": {"equals": False},
    }
    assert _evaluate({"summary": {"success": 1.0, "failures": 0}, "holdout_used": False}, acceptance) == []
    failures = _evaluate({"summary": {"success": 0.9, "failures": 1}, "holdout_used": True}, acceptance)
    assert len(failures) == 3


def test_numeric_baseline_comparison_reports_only_shared_numbers() -> None:
    assert _numeric_deltas(
        {"summary": {"accuracy": 1.0, "label": "new"}},
        {"summary": {"accuracy": 0.75, "label": "old"}},
    ) == {"summary.accuracy": 0.25}
