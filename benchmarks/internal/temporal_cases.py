from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path


DATASET = Path(__file__).parent / "datasets" / "temporal" / "development" / "development_v1.jsonl"


@dataclass(frozen=True, slots=True)
class TemporalCase:
    id: str
    area: str
    level: str
    test_node: str
    validates: tuple[str, ...]


def load_temporal_cases() -> list[TemporalCase]:
    cases = []
    for line in DATASET.read_text(encoding="utf-8").splitlines():
        if line.strip():
            row = json.loads(line)
            cases.append(TemporalCase(row["id"], row["area"], row["level"], row["test_node"], tuple(row["validates"])))
    return cases
