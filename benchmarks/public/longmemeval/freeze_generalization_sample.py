from __future__ import annotations

import argparse
import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

from benchmarks.public.longmemeval.contract import load_dataset
from benchmarks.public.longmemeval.episodic_shadow import FROZEN_QUESTION_IDS

SAMPLE_SEED = "lme-role-aware-generalization-v1"
CASES_PER_TYPE = 5


def _order_key(question_id: str) -> str:
    return hashlib.sha256(f"{SAMPLE_SEED}:{question_id}".encode()).hexdigest()


def freeze(dataset: Path) -> dict[str, object]:
    cases, dataset_sha256 = load_dataset(dataset)
    question_types = sorted({case.question_type for case in cases})
    selected = []
    for question_type in question_types:
        eligible = [
            case
            for case in cases
            if case.question_type == question_type
            and case.question_id not in FROZEN_QUESTION_IDS
        ]
        abstentions = sorted(
            (case for case in eligible if case.question_id.endswith("_abs")),
            key=lambda case: _order_key(case.question_id),
        )
        answerable = sorted(
            (case for case in eligible if not case.question_id.endswith("_abs")),
            key=lambda case: _order_key(case.question_id),
        )
        abstention_slots = 1 if abstentions else 0
        selected.extend(answerable[: CASES_PER_TYPE - abstention_slots])
        selected.extend(abstentions[:abstention_slots])

    return {
        "schema_version": "longmemeval-public-development-sample-v1",
        "created_at": datetime.now(UTC).isoformat(),
        "classification": "public-development-generalization",
        "selection_inputs": ["question_id", "question_type", "abstention_suffix"],
        "selection_excludes_answers_and_answer_sessions": True,
        "seed": SAMPLE_SEED,
        "dataset_sha256": dataset_sha256,
        "pilot_question_ids_excluded": sorted(FROZEN_QUESTION_IDS),
        "case_count": len(selected),
        "abstention_count": sum(
            case.question_id.endswith("_abs") for case in selected
        ),
        "question_type_counts": {
            question_type: sum(
                case.question_type == question_type for case in selected
            )
            for question_type in question_types
        },
        "question_ids": [case.question_id for case in selected],
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    manifest = freeze(args.dataset)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print(json.dumps(manifest, indent=2))


if __name__ == "__main__":
    main()
