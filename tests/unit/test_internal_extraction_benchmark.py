from __future__ import annotations

import json
from pathlib import Path

import pytest

from benchmarks.internal.cases import ExpectedMemory, load_cases, load_legacy_cases
from benchmarks.internal.matching import match_memories
from benchmarks.internal.metrics import aggregate_metrics, evaluate_extraction
from benchmarks.internal.results import compare_baseline, write_run_record
from benchmarks.internal.runner import evaluate_predictions, expected_output_predictions

ROOT = Path(__file__).resolve().parents[2]
DATASET_ROOT = ROOT / "benchmarks" / "internal" / "datasets" / "extraction"
DEVELOPMENT_ROOT = DATASET_ROOT / "development"
HOLDOUT_ROOT = DATASET_ROOT / "holdout"
LEGACY_ROOT = ROOT / "tests" / "evals" / "general_extraction_cases"
BASELINE_PATH = ROOT / "benchmarks" / "internal" / "baselines" / "baseline-index.json"


def test_internal_dataset_has_separate_development_and_holdout_splits() -> None:
    new_cases = load_cases(DEVELOPMENT_ROOT)
    legacy_cases = load_legacy_cases(LEGACY_ROOT)

    assert len(legacy_cases) == 16
    assert len(new_cases) == 33
    assert all(case.split == "development" for case in new_cases)
    assert len({case.id for case in [*legacy_cases, *new_cases]}) == 49
    assert HOLDOUT_ROOT.is_dir()


def test_holdout_requires_explicit_manual_approval(monkeypatch) -> None:
    monkeypatch.delenv("MEMORYOS_HOLDOUT_APPROVAL", raising=False)
    with pytest.raises(PermissionError, match="Holdout access is locked"):
        load_cases(HOLDOUT_ROOT)


def test_dataset_covers_safety_thresholds_and_all_categories() -> None:
    cases = [*load_legacy_cases(LEGACY_ROOT), *load_cases(DEVELOPMENT_ROOT)]
    categories = {memory.category for case in cases for memory in case.expected_memories}

    assert categories == {"preference", "fact", "goal", "procedure", "relationship", "expertise"}
    assert any(memory.disposition == "pending" for case in cases for memory in case.expected_memories)
    assert any(case.forbidden_patterns for case in cases)
    assert {"secret", "temporal"} <= {tag for case in cases for tag in case.tags}
    assert all(
        memory.evidence_turns
        for case in cases
        for memory in case.expected_memories
    )


def test_one_to_one_metrics_penalize_extra_memories() -> None:
    case = next(case for case in load_cases(DEVELOPMENT_ROOT) if case.id == "dev_explicit_location")
    predictions = expected_output_predictions([case])[case.id]
    predictions.append(
        {
            "content": "User owns a restaurant.",
            "category": "fact",
            "disposition": "store",
            "importance_score": 7.0,
            "confidence": 0.9,
        }
    )

    metrics = evaluate_extraction(case, predictions)

    assert metrics.recall == 1.0
    assert metrics.precision == pytest.approx(2 / 3)
    assert metrics.false_memory_rate == pytest.approx(1 / 3)
    assert metrics.f1 < 1.0


def test_relaxed_match_requires_same_category_anchors_and_polarity() -> None:
    expected = (
        ExpectedMemory(
            proposition="User may prefer short replies, but this may only apply today.",
            category="preference",
        ),
    )
    equivalent = [
        {
            "content": "User prefers short replies for this session.",
            "category": "preference",
        }
    ]

    matches = match_memories(expected, equivalent)

    assert len(matches) == 1
    assert matches[0].method == "relaxed_same_category"
    assert match_memories(
        expected,
        [{"content": equivalent[0]["content"], "category": "goal"}],
    ) == []
    assert match_memories(
        expected,
        [{"content": "User does not prefer short replies today.", "category": "preference"}],
    ) == []


def test_safety_leak_is_a_separate_hard_metric() -> None:
    case = next(case for case in load_cases(DEVELOPMENT_ROOT) if case.id == "dev_secret_with_valid_fact")
    metrics = evaluate_extraction(
        case,
        [{"content": "User token is ghp_exampleSecret123.", "category": "fact", "confidence": 0.99}],
    )

    assert metrics.forbidden_leak_count == 1
    assert metrics.forbidden_leak_rate == 1.0
    assert metrics.false_memory_rate == 1.0


def test_evidence_accuracy_and_confidence_calibration_are_reported() -> None:
    case = next(case for case in load_cases(DEVELOPMENT_ROOT) if case.id == "dev_explicit_location")
    prediction = expected_output_predictions([case])[case.id][0]

    correct = evaluate_extraction(case, [prediction])
    assert correct.evidence_accuracy == 1.0
    assert 0.0 <= correct.confidence_ece <= 1.0

    prediction["evidence_turns"] = [99]
    wrong = evaluate_extraction(case, [prediction])
    assert wrong.evidence_accuracy == 0.0


def test_deterministic_contract_baseline_and_machine_readable_artifact(tmp_path: Path) -> None:
    cases = [*load_legacy_cases(LEGACY_ROOT), *load_cases(DEVELOPMENT_ROOT)]
    predictions = expected_output_predictions(cases)
    record = evaluate_predictions(cases, predictions, config={"mode": "expected-output-contract"})
    baseline = json.loads(BASELINE_PATH.read_text(encoding="utf-8"))

    assert record["summary"]["case_count"] == 49
    assert compare_baseline(record["summary"], baseline) == []
    assert record["summary"]["micro_precision"] == 1.0
    assert record["summary"]["micro_recall"] == 1.0
    assert record["summary"]["evidence_accuracy"] == 1.0
    assert 0.0 <= record["summary"]["confidence_ece"] <= 1.0
    assert record["summary"]["evaluator_diagnostics"]["missing_evidence_annotation_count"] == 0
    assert set(record["summary"]["metric_groups"]) == {
        "extraction_quality",
        "calibration",
        "evidence_provenance",
        "evaluator_annotation_diagnostics",
    }
    assert record["summary"]["forbidden_leak_count"] == 0

    output = tmp_path / "run.json"
    write_run_record(record, output)
    stored = json.loads(output.read_text(encoding="utf-8"))
    assert stored["schema_version"] == "1.0"
    assert stored["dataset_hash"] == record["dataset_hash"]
    assert len(stored["cases"]) == 49


def test_aggregate_metrics_are_micro_and_macro_aware() -> None:
    cases = load_cases(DATASET_ROOT / "development")[:2]
    predictions = expected_output_predictions(cases)
    results = [evaluate_extraction(case, predictions[case.id]) for case in cases]
    summary = aggregate_metrics(results)

    assert summary["micro_f1"] == 1.0
    assert summary["macro_f1"] == 1.0
