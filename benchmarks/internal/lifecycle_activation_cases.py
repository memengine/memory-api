from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path


DATASET = Path(__file__).parent / "datasets/lifecycle_activation/development/development_v1.jsonl"


@dataclass(frozen=True, slots=True)
class LifecycleActivationCase:
    id: str
    area: str
    level: str
    test_node: str
    validates: tuple[str, ...]


def load_lifecycle_activation_cases() -> list[LifecycleActivationCase]:
    return [
        LifecycleActivationCase(
            id=row["id"], area=row["area"], level=row["level"],
            test_node=row["test_node"], validates=tuple(row["validates"]),
        )
        for line in DATASET.read_text(encoding="utf-8").splitlines()
        if (row := json.loads(line))
    ]
