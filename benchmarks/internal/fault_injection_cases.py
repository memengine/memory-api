from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path


ROOT = Path(__file__).parent / "datasets"
DATASETS = (
    ROOT / "integration_reliability" / "development" / "development_v1.jsonl",
    ROOT / "fault_injection" / "development" / "extension_v3.jsonl",
)


@dataclass(frozen=True, slots=True)
class FaultCase:
    id: str
    area: str
    level: str
    test_node: str
    validates: tuple[str, ...]


def load_fault_cases() -> list[FaultCase]:
    cases = []
    for path in DATASETS:
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            row = json.loads(line)
            cases.append(FaultCase(row["id"], row["area"], row["level"], row["test_node"], tuple(row["validates"])))
    return cases
