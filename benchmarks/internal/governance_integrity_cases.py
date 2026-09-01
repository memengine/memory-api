from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path


ROOT = Path(__file__).parent / "datasets"
DATASETS = (
    ROOT / "lifecycle_provenance" / "development" / "development_v1.jsonl",
    ROOT / "governance_integrity" / "development" / "extension_v2.jsonl",
)


@dataclass(frozen=True, slots=True)
class GovernanceIntegrityCase:
    id: str
    area: str
    level: str
    test_node: str
    validates: tuple[str, ...]


def load_governance_integrity_cases() -> list[GovernanceIntegrityCase]:
    cases: list[GovernanceIntegrityCase] = []
    for dataset in DATASETS:
        for line in dataset.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            row = json.loads(line)
            cases.append(GovernanceIntegrityCase(
                id=row["id"], area=row["area"], level=row["level"],
                test_node=row["test_node"], validates=tuple(row["validates"]),
            ))
    return cases
