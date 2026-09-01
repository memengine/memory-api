from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path


DATASET = Path(__file__).parent / "datasets" / "integration_reliability" / "development" / "development_v1.jsonl"


@dataclass(frozen=True, slots=True)
class IntegrationReliabilityCase:
    id: str
    area: str
    level: str
    test_node: str
    validates: tuple[str, ...]


def load_integration_reliability_cases() -> list[IntegrationReliabilityCase]:
    cases: list[IntegrationReliabilityCase] = []
    for line in DATASET.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        cases.append(
            IntegrationReliabilityCase(
                id=row["id"], area=row["area"], level=row["level"],
                test_node=row["test_node"], validates=tuple(row["validates"]),
            )
        )
    return cases
