from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from benchmarks.internal.cases import HOLDOUT_APPROVAL_ENV, HOLDOUT_APPROVAL_TOKEN
from benchmarks.internal.phase3a_confirmation import run_approved_holdout_evaluation


def dataset_sha256(dataset: Path) -> str:
    digest = hashlib.sha256()
    with dataset.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _validated_expected_sha256(value: str) -> str:
    normalized = value.strip().lower()
    if len(normalized) != 64 or any(character not in "0123456789abcdef" for character in normalized):
        raise ValueError("Expected dataset SHA-256 must be 64 hexadecimal characters.")
    return normalized


def _write_marker(marker: Path, payload: dict[str, Any]) -> None:
    marker.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _write_output_exclusive(output: Path, record: dict[str, Any]) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    try:
        with output.open("x", encoding="utf-8") as stream:
            json.dump(record, stream, indent=2, sort_keys=True)
            stream.write("\n")
    except FileExistsError as exc:
        raise FileExistsError(f"Holdout output already exists: {output}") from exc


def claim_holdout_once(dataset: Path, *, expected_sha256: str) -> Path:
    expected = _validated_expected_sha256(expected_sha256)
    observed = dataset_sha256(dataset)
    if observed != expected:
        raise ValueError(
            f"Holdout SHA-256 mismatch: expected {expected}, observed {observed}."
        )
    marker = Path(f"{dataset}.consumed.json")
    try:
        with marker.open("x", encoding="utf-8") as stream:
            json.dump(
                {
                    "status": "claimed",
                    "dataset_sha256": observed,
                    "claimed_at": datetime.now(UTC).isoformat(),
                },
                stream,
                indent=2,
                sort_keys=True,
            )
            stream.write("\n")
    except FileExistsError as exc:
        raise RuntimeError(f"Holdout has already been consumed: {dataset}") from exc
    return marker


async def run_sealed_holdout_once(
    *,
    dataset: Path,
    output: Path,
    expected_sha256: str,
) -> dict[str, Any]:
    if os.getenv(HOLDOUT_APPROVAL_ENV) != HOLDOUT_APPROVAL_TOKEN:
        raise PermissionError(
            f"Holdout is locked without {HOLDOUT_APPROVAL_ENV} approval."
        )
    if output.exists():
        raise FileExistsError(f"Holdout output already exists: {output}")

    expected = _validated_expected_sha256(expected_sha256)
    marker = claim_holdout_once(dataset, expected_sha256=expected)
    claimed_at = json.loads(marker.read_text(encoding="utf-8"))["claimed_at"]
    try:
        record = await run_approved_holdout_evaluation(dataset)
        observed_after_run = dataset_sha256(dataset)
        if observed_after_run != expected:
            raise RuntimeError("Holdout dataset changed after it was claimed.")
        record.setdefault("config", {})["dataset_sha256"] = expected
        _write_output_exclusive(output, record)
    except BaseException as exc:
        _write_marker(
            marker,
            {
                "status": "failed",
                "dataset_sha256": expected,
                "claimed_at": claimed_at,
                "completed_at": datetime.now(UTC).isoformat(),
                "error_type": type(exc).__name__,
            },
        )
        raise

    _write_marker(
        marker,
        {
            "status": "completed",
            "dataset_sha256": expected,
            "claimed_at": claimed_at,
            "completed_at": datetime.now(UTC).isoformat(),
            "run_id": record["run_id"],
            "artifact": str(output),
            "release_eligible": bool(record["summary"]["release_eligible"]),
        },
    )
    return record


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the sealed Phase 3A holdout once.")
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--expected-sha256", required=True)
    parser.add_argument("--allow-holdout", action="store_true")
    args = parser.parse_args()
    if not args.allow_holdout:
        raise SystemExit("Holdout is locked: --allow-holdout is required.")
    record = asyncio.run(
        run_sealed_holdout_once(
            dataset=args.dataset,
            output=args.output,
            expected_sha256=args.expected_sha256,
        )
    )
    print(json.dumps({"output": str(args.output), "summary": record["summary"]}, indent=2))


if __name__ == "__main__":
    main()
