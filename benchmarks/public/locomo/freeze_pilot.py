from __future__ import annotations

import argparse
import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from benchmarks.public.locomo.contract import load_dataset

PILOT_SEED = "memoryos-locomo-pilot-v1"
CONVERSATION_COUNT = 2
QUESTIONS_PER_CATEGORY = 5


def _order_key(kind: str, identifier: str) -> str:
    return hashlib.sha256(f"{PILOT_SEED}:{kind}:{identifier}".encode()).hexdigest()


def freeze(dataset: Path) -> dict[str, Any]:
    samples, dataset_sha256 = load_dataset(dataset)
    selected_samples = sorted(
        samples,
        key=lambda sample: _order_key("sample", sample.sample_id),
    )[:CONVERSATION_COUNT]

    selected_questions: list[dict[str, Any]] = []
    for category in range(1, 6):
        eligible = [
            (sample, index)
            for sample in selected_samples
            for index, question in enumerate(sample.qa)
            if question.category == category
        ]
        ordered = sorted(
            eligible,
            key=lambda item: _order_key("question", item[0].question_id(item[1])),
        )
        if len(ordered) < QUESTIONS_PER_CATEGORY:
            raise ValueError(
                f"selected conversations have only {len(ordered)} category {category} questions"
            )
        selected_questions.extend(
            {
                "question_id": sample.question_id(index),
                "sample_id": sample.sample_id,
                "qa_index": index,
                "category": category,
            }
            for sample, index in ordered[:QUESTIONS_PER_CATEGORY]
        )

    return {
        "schema_version": "memoryos-locomo-public-pilot-v1",
        "created_at": datetime.now(UTC).isoformat(),
        "classification": "public-benchmark-pilot-not-official-score",
        "seed": PILOT_SEED,
        "dataset_sha256": dataset_sha256,
        "selection_inputs": ["sample_id", "qa_index", "category"],
        "selection_excludes_answers_and_evidence": True,
        "conversation_count": len(selected_samples),
        "sample_ids": [sample.sample_id for sample in selected_samples],
        "question_count": len(selected_questions),
        "category_counts": {
            str(category): sum(
                item["category"] == category for item in selected_questions
            )
            for category in range(1, 6)
        },
        "questions": selected_questions,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Freeze the MemoryOS LoCoMo pilot")
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    manifest = freeze(args.dataset)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print(json.dumps(manifest, indent=2))


if __name__ == "__main__":
    main()
