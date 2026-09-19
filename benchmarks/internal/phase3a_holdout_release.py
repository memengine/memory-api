from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

from benchmarks.internal.phase3a_confirmation import run_approved_holdout_evaluation
from benchmarks.internal.results import write_run_record

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_HOLDOUT = (
    ROOT
    / "benchmarks"
    / "internal"
    / "datasets"
    / "phase3a_confirmation"
    / "holdout"
    / "holdout_v1.json"
)
DEFAULT_OUTPUT = ROOT / "artifacts" / "internal-benchmarks" / "phase3a" / "phase3a-holdout-v1.json"


def claim_holdout_once(dataset: Path) -> Path:
    marker = Path(f"{dataset}.consumed.json")
    try:
        with marker.open("x", encoding="utf-8") as stream:
            json.dump({"status": "claimed"}, stream)
            stream.write("\n")
    except FileExistsError as exc:
        raise RuntimeError(f"Holdout has already been consumed: {dataset}") from exc
    return marker


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the sealed Phase 3A holdout once.")
    parser.add_argument("--dataset", type=Path, default=DEFAULT_HOLDOUT)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--allow-holdout", action="store_true")
    args = parser.parse_args()
    if not args.allow_holdout:
        raise SystemExit("Holdout is locked: --allow-holdout is required.")
    marker = claim_holdout_once(args.dataset)
    record = asyncio.run(run_approved_holdout_evaluation(args.dataset))
    write_run_record(record, args.output)
    marker.write_text(
        json.dumps(
            {
                "status": "completed",
                "run_id": record["run_id"],
                "artifact": str(args.output),
                "release_eligible": bool(record["summary"]["release_eligible"]),
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    print(json.dumps({"output": str(args.output), "summary": record["summary"]}, indent=2))


if __name__ == "__main__":
    main()
