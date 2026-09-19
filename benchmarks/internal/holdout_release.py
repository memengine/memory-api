from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
from typing import Any

from benchmarks.internal.cases import ExtractionCase, load_cases
from benchmarks.internal.live_provider import run_live_case_evaluation
from benchmarks.internal.results import write_run_record

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_HOLDOUT = ROOT / "benchmarks" / "internal" / "datasets" / "extraction" / "holdout" / "holdout_v1.jsonl"
DEFAULT_OUTPUT_ROOT = ROOT / "artifacts" / "internal-benchmarks" / "holdout"


async def run_approved_holdout_evaluation(dataset: str | Path) -> dict[str, Any]:
    """Run the sealed set only after the loader's dual approval gate succeeds."""

    cases: list[ExtractionCase] = load_cases(dataset, allow_holdout=True)
    if not cases or any(case.split != "holdout" for case in cases):
        raise ValueError("Approved holdout runner accepts only a non-empty holdout dataset.")
    return await run_live_case_evaluation(
        cases,
        mode="live-provider-sealed-holdout-manual-only",
        holdout_loaded=True,
        proposal_confirmation_enabled=True,
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run the sealed holdout only with an explicitly approved manual release." 
    )
    parser.add_argument("--dataset", type=Path, default=DEFAULT_HOLDOUT)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--allow-holdout", action="store_true")
    args = parser.parse_args()
    if not args.allow_holdout:
        raise SystemExit("Holdout is locked: --allow-holdout is required.")
    record = asyncio.run(run_approved_holdout_evaluation(args.dataset))
    output = args.output or DEFAULT_OUTPUT_ROOT / f"{record['run_id']}.json"
    write_run_record(record, output)
    # Never echo case content, expected memories, or aggregate outputs to logs.
    print(json.dumps({"output": str(output), "summary": record["summary"]}, indent=2))


if __name__ == "__main__":
    main()
