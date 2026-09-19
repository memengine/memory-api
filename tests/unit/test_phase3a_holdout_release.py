import asyncio

import pytest

from benchmarks.internal.cases import HOLDOUT_APPROVAL_ENV, HOLDOUT_APPROVAL_TOKEN
from benchmarks.internal.phase3a_confirmation import (
    load_confirmation_cases,
    run_approved_holdout_evaluation,
)


def test_phase3a_holdout_loader_requires_approval(monkeypatch, tmp_path) -> None:
    path = tmp_path / "holdout.json"
    path.write_text('{"split":"holdout","reference_types":{}}', encoding="utf-8")
    monkeypatch.delenv(HOLDOUT_APPROVAL_ENV, raising=False)

    with pytest.raises(PermissionError, match="holdout is locked"):
        load_confirmation_cases(path, expected_split="holdout")


def test_phase3a_holdout_runner_marks_holdout_mode(monkeypatch) -> None:
    captured = {}

    async def fake_run(dataset, *, expected_split, provider_attempts):
        captured.update(
            dataset=dataset,
            expected_split=expected_split,
            provider_attempts=provider_attempts,
        )
        return {"summary": {"release_eligible": True}}

    monkeypatch.setenv(HOLDOUT_APPROVAL_ENV, HOLDOUT_APPROVAL_TOKEN)
    monkeypatch.setattr(
        "benchmarks.internal.phase3a_confirmation.run_confirmation_evaluation",
        fake_run,
    )

    result = asyncio.run(run_approved_holdout_evaluation("sealed.json", provider_attempts=2))

    assert result["summary"]["release_eligible"] is True
    assert captured == {
        "dataset": "sealed.json",
        "expected_split": "holdout",
        "provider_attempts": 2,
    }
