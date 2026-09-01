from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any


DATASET = Path(__file__).parent / "datasets" / "conflict" / "development" / "development_v1.jsonl"


@dataclass(frozen=True, slots=True)
class ConflictCase:
    id: str
    scenario_type: str
    events: tuple[dict[str, Any], ...]
    expected: dict[str, Any]


def load_conflict_development_cases(path: Path = DATASET) -> list[ConflictCase]:
    cases: list[ConflictCase] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        raw = json.loads(line)
        cases.append(
            ConflictCase(
                id=str(raw["id"]),
                scenario_type=str(raw["scenario_type"]),
                events=tuple(raw["events"]),
                expected=dict(raw["expected"]),
            )
        )
    return cases


__all__ = ["ConflictCase", "load_conflict_development_cases"]

