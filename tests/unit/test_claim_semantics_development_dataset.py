from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DATASET = (
    ROOT
    / "benchmarks"
    / "internal"
    / "datasets"
    / "claim_semantics"
    / "development"
    / "development_v1.json"
)


def test_claim_semantics_development_dataset_has_balanced_required_slices() -> None:
    payload = json.loads(DATASET.read_text(encoding="utf-8"))
    cases = payload["cases"]

    assert payload["schema_version"] == "1.0"
    assert payload["split"] == "development"
    assert len(cases) == 18
    assert len({case["id"] for case in cases}) == len(cases)
    assert Counter(case["language"] for case in cases) == {
        "en": 6,
        "hi": 6,
        "hinglish": 6,
    }
    assert Counter(case["expected"]["speech_act"] for case in cases) == {
        "assertion": 6,
        "correction": 3,
        "uncertain_change": 3,
        "retraction": 3,
        "reaffirmation": 3,
    }
    assert all(case["utterance"].strip() for case in cases)
    assert all(case["expected"]["predicate"] for case in cases)
