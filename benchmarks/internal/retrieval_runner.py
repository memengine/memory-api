from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path

from benchmarks.internal.retrieval_cases import load_retrieval_development_cases
from benchmarks.internal.retrieval_eval import aggregate, evaluate_case


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    cases = load_retrieval_development_cases()
    rows = [evaluate_case(case) for case in cases]
    payload = {
        "benchmark": "retrieval-correctness-development-v1",
        "captured_at": datetime.now(UTC).isoformat(),
        "evaluation_layer": "production_hybrid_ranker_with_controlled_vector_scores",
        "holdout_used": False, "production_behavior_changed": False,
        "summary": aggregate(rows), "cases": rows,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(json.dumps(payload["summary"], indent=2))


if __name__ == "__main__":
    main()
