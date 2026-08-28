from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path

from benchmarks.public.longmemeval.contract import load_dataset
from benchmarks.public.longmemeval.episodic_chunk_selection import (
    MAX_ACCEPTABLE_FILLER_PER_CASE,
    _select_cases,
)
from benchmarks.public.longmemeval.episodic_selection import _evaluate_candidate


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    all_cases, dataset_sha256 = load_dataset(args.dataset)
    cases, _ = _select_cases(all_cases, dataset_sha256, args.manifest)
    source = json.loads(args.input.read_text(encoding="utf-8"))
    scored = source["scored_results"]
    cutoffs = source["selection_policy"]["candidate_cutoffs"]
    candidates = [
        _evaluate_candidate(cases, scored, float(cutoff)) for cutoff in cutoffs
    ]
    filler_limit = int(len(cases) * MAX_ACCEPTABLE_FILLER_PER_CASE)
    passing = [
        candidate
        for candidate in candidates
        if candidate["answerable_recall_at_k"] == 1.0
        and candidate["abstention_evidence_recall_at_k"] == 1.0
        and candidate["valid_memories_incorrectly_removed"] == 0
        and candidate["irrelevant_filler_results"] <= filler_limit
    ]
    selected = max(
        passing,
        key=lambda candidate: (
            candidate["answerable_precision_at_k"],
            candidate["ndcg"],
            candidate["cutoff"],
        ),
        default=None,
    )
    source.update(
        {
            "schema_version": "longmemeval-episodic-role-generalization-v2",
            "reevaluated_at": datetime.now(UTC).isoformat(),
            "evaluation_contract": {
                "abstention_semantics": (
                    "gold evidence must be retrieved; answer abstention belongs to QA"
                ),
                "explicit_month_filter": False,
                "filler_limit": filler_limit,
            },
            "experiment_accepted": selected is not None,
            "experiment_rejection_reason": None
            if selected is not None
            else (
                "No cutoff satisfies frozen evidence recall, valid-removal, and filler "
                "criteria under the corrected abstention contract."
            ),
            "candidates": candidates,
            "selected": selected,
        }
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(source, indent=2), encoding="utf-8")
    print(
        json.dumps(
            {
                "output": str(args.output),
                "selected": selected,
                "provider_calls": 0,
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
