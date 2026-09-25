import asyncio
import hashlib
import json

import pytest

from benchmarks.internal.cases import HOLDOUT_APPROVAL_ENV, HOLDOUT_APPROVAL_TOKEN
from benchmarks.internal.phase3a_holdout_release import (
    claim_holdout_once,
    run_sealed_holdout_once,
)


def _sha256(path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _allow_valid_preflight(monkeypatch) -> None:
    monkeypatch.setattr(
        "benchmarks.internal.phase3a_holdout_release.load_confirmation_cases",
        lambda _dataset, *, expected_split: ([], {})
        if expected_split == "holdout"
        else None,
    )


def test_phase3a_holdout_claim_is_single_use(tmp_path) -> None:
    dataset = tmp_path / "holdout.json"
    dataset.write_text("{}", encoding="utf-8")
    expected_sha256 = _sha256(dataset)

    marker = claim_holdout_once(dataset, expected_sha256=expected_sha256)

    assert marker.exists()
    assert json.loads(marker.read_text(encoding="utf-8"))["dataset_sha256"] == expected_sha256
    with pytest.raises(RuntimeError, match="already been consumed"):
        claim_holdout_once(dataset, expected_sha256=expected_sha256)


def test_phase3a_holdout_hash_mismatch_does_not_consume_dataset(tmp_path) -> None:
    dataset = tmp_path / "holdout.json"
    dataset.write_text("{}", encoding="utf-8")

    with pytest.raises(ValueError, match="SHA-256 mismatch"):
        claim_holdout_once(dataset, expected_sha256="0" * 64)

    assert not (tmp_path / "holdout.json.consumed.json").exists()


def test_phase3a_holdout_without_approval_is_not_consumed(monkeypatch, tmp_path) -> None:
    dataset = tmp_path / "holdout.json"
    output = tmp_path / "result.json"
    dataset.write_text('{"split":"holdout"}', encoding="utf-8")
    monkeypatch.delenv(HOLDOUT_APPROVAL_ENV, raising=False)

    with pytest.raises(PermissionError, match=HOLDOUT_APPROVAL_ENV):
        asyncio.run(
            run_sealed_holdout_once(
                dataset=dataset,
                output=output,
                expected_sha256=_sha256(dataset),
            )
        )

    assert not (tmp_path / "holdout.json.consumed.json").exists()


def test_phase3a_existing_output_does_not_consume_dataset(monkeypatch, tmp_path) -> None:
    dataset = tmp_path / "holdout.json"
    output = tmp_path / "result.json"
    dataset.write_text('{"split":"holdout"}', encoding="utf-8")
    output.write_text("do not overwrite", encoding="utf-8")
    monkeypatch.setenv(HOLDOUT_APPROVAL_ENV, HOLDOUT_APPROVAL_TOKEN)

    with pytest.raises(FileExistsError, match="output already exists"):
        asyncio.run(
            run_sealed_holdout_once(
                dataset=dataset,
                output=output,
                expected_sha256=_sha256(dataset),
            )
        )

    assert output.read_text(encoding="utf-8") == "do not overwrite"
    assert not (tmp_path / "holdout.json.consumed.json").exists()


def test_phase3a_invalid_schema_does_not_consume_dataset(monkeypatch, tmp_path) -> None:
    dataset = tmp_path / "holdout.json"
    output = tmp_path / "result.json"
    dataset.write_text(
        '{"schema_version":"2.0","reference_types":{}}',
        encoding="utf-8",
    )
    monkeypatch.setenv(HOLDOUT_APPROVAL_ENV, HOLDOUT_APPROVAL_TOKEN)

    with pytest.raises(ValueError, match="expected holdout data"):
        asyncio.run(
            run_sealed_holdout_once(
                dataset=dataset,
                output=output,
                expected_sha256=_sha256(dataset),
            )
        )

    assert not (tmp_path / "holdout.json.consumed.json").exists()
    assert not output.exists()


def test_phase3a_failed_holdout_run_remains_consumed(monkeypatch, tmp_path) -> None:
    dataset = tmp_path / "holdout.json"
    output = tmp_path / "result.json"
    dataset.write_text('{"split":"holdout"}', encoding="utf-8")
    expected_sha256 = _sha256(dataset)

    async def fail(_dataset):
        raise RuntimeError("provider failed")

    monkeypatch.setenv(HOLDOUT_APPROVAL_ENV, HOLDOUT_APPROVAL_TOKEN)
    _allow_valid_preflight(monkeypatch)
    monkeypatch.setattr(
        "benchmarks.internal.phase3a_holdout_release.run_approved_holdout_evaluation",
        fail,
    )

    with pytest.raises(RuntimeError, match="provider failed"):
        asyncio.run(
            run_sealed_holdout_once(
                dataset=dataset,
                output=output,
                expected_sha256=expected_sha256,
            )
        )

    marker = json.loads(
        (tmp_path / "holdout.json.consumed.json").read_text(encoding="utf-8")
    )
    assert marker["status"] == "failed"
    assert marker["dataset_sha256"] == expected_sha256
    assert marker["error_type"] == "RuntimeError"
    assert not output.exists()


def test_phase3a_successful_holdout_records_hash_and_artifact(monkeypatch, tmp_path) -> None:
    dataset = tmp_path / "holdout.json"
    output = tmp_path / "result.json"
    dataset.write_text('{"split":"holdout"}', encoding="utf-8")
    expected_sha256 = _sha256(dataset)
    record = {
        "run_id": "holdout-run-2",
        "summary": {"release_eligible": True},
    }

    async def succeed(_dataset):
        return record

    monkeypatch.setenv(HOLDOUT_APPROVAL_ENV, HOLDOUT_APPROVAL_TOKEN)
    _allow_valid_preflight(monkeypatch)
    monkeypatch.setattr(
        "benchmarks.internal.phase3a_holdout_release.run_approved_holdout_evaluation",
        succeed,
    )

    result = asyncio.run(
        run_sealed_holdout_once(
            dataset=dataset,
            output=output,
            expected_sha256=expected_sha256,
        )
    )

    marker = json.loads(
        (tmp_path / "holdout.json.consumed.json").read_text(encoding="utf-8")
    )
    assert result == record
    assert output.exists()
    assert record["config"]["dataset_sha256"] == expected_sha256
    assert marker["status"] == "completed"
    assert marker["dataset_sha256"] == expected_sha256
    assert marker["artifact"] == str(output)
    assert marker["release_eligible"] is True


def test_phase3a_mutated_holdout_fails_closed(monkeypatch, tmp_path) -> None:
    dataset = tmp_path / "holdout.json"
    output = tmp_path / "result.json"
    dataset.write_text('{"split":"holdout"}', encoding="utf-8")
    expected_sha256 = _sha256(dataset)

    async def mutate(source):
        source.write_text('{"split":"holdout","changed":true}', encoding="utf-8")
        return {"run_id": "mutated", "summary": {"release_eligible": True}}

    monkeypatch.setenv(HOLDOUT_APPROVAL_ENV, HOLDOUT_APPROVAL_TOKEN)
    _allow_valid_preflight(monkeypatch)
    monkeypatch.setattr(
        "benchmarks.internal.phase3a_holdout_release.run_approved_holdout_evaluation",
        mutate,
    )

    with pytest.raises(RuntimeError, match="changed after it was claimed"):
        asyncio.run(
            run_sealed_holdout_once(
                dataset=dataset,
                output=output,
                expected_sha256=expected_sha256,
            )
        )

    marker = json.loads(
        (tmp_path / "holdout.json.consumed.json").read_text(encoding="utf-8")
    )
    assert marker["status"] == "failed"
    assert marker["error_type"] == "RuntimeError"
    assert not output.exists()
