from __future__ import annotations

import asyncio

import pytest

from benchmarks.internal.cases import ExtractionCase
from benchmarks.internal.holdout_release import run_approved_holdout_evaluation


def _case(split: str) -> ExtractionCase:
    return ExtractionCase(
        id="sealed-case",
        split=split,
        case_type="preference",
        messages=({"role": "user", "content": "Keep answers concise."},),
        expected_memories=(),
    )


def test_manual_holdout_runner_uses_shared_production_path(monkeypatch, tmp_path) -> None:
    captured = {}

    monkeypatch.setattr(
        "benchmarks.internal.holdout_release.load_cases",
        lambda path, allow_holdout: [_case("holdout")],
    )

    async def fake_run(cases, *, mode, holdout_loaded, proposal_confirmation_enabled):
        captured.update(
            {
                "cases": cases,
                "mode": mode,
                "holdout_loaded": holdout_loaded,
                "proposal_confirmation_enabled": proposal_confirmation_enabled,
            }
        )
        return {"run_id": "holdout-run", "summary": {}}

    monkeypatch.setattr("benchmarks.internal.holdout_release.run_live_case_evaluation", fake_run)

    result = asyncio.run(run_approved_holdout_evaluation(tmp_path / "holdout_v1.jsonl"))

    assert result["run_id"] == "holdout-run"
    assert captured["mode"] == "live-provider-sealed-holdout-manual-only"
    assert captured["holdout_loaded"] is True
    assert captured["proposal_confirmation_enabled"] is True
    assert captured["cases"][0].split == "holdout"


def test_manual_holdout_runner_rejects_non_holdout_cases(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(
        "benchmarks.internal.holdout_release.load_cases",
        lambda path, allow_holdout: [_case("development")],
    )

    with pytest.raises(ValueError, match="holdout dataset"):
        asyncio.run(run_approved_holdout_evaluation(tmp_path / "development_v1.jsonl"))
