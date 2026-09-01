from __future__ import annotations

import json
from pathlib import Path


DATASET = Path("benchmarks/internal/datasets/agent_deletion/development/development_v1.jsonl")


def test_frozen_agent_deletion_pack_has_required_unique_scenarios() -> None:
    cases=[json.loads(line) for line in DATASET.read_text(encoding="utf-8").splitlines() if line]
    assert len(cases)==12
    assert len({case["id"] for case in cases})==12
    assert {"security","correctness","auditability","reliability","privacy"}=={case["risk"] for case in cases}


def test_agent_deletion_pack_is_development_only() -> None:
    assert "development" in DATASET.parts
    assert "holdout" not in DATASET.parts
