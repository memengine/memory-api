from __future__ import annotations

import json
from pathlib import Path


DATASET = Path("benchmarks/internal/datasets/multi_agent/development/development_v1.jsonl")


def _cases():
    return [json.loads(line) for line in DATASET.read_text(encoding="utf-8").splitlines() if line]


def test_frozen_multi_agent_development_pack_has_unique_required_scenarios() -> None:
    cases = _cases()
    ids = {case["id"] for case in cases}
    assert len(cases) == 12
    assert len(ids) == len(cases)
    assert {case["plane"] for case in cases} == {"tenant", "passport"}
    assert {
        "agent_filter", "expected_shared_visibility", "cross_user_leakage",
        "cross_tenant_leakage", "category_grant", "revocation",
        "write_authorization", "provenance",
    } <= {case["metric"] for case in cases}


def test_multi_agent_pack_is_development_only() -> None:
    assert "development" in DATASET.parts
    assert "holdout" not in DATASET.parts
