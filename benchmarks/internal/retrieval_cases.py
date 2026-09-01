from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any


DATASET = Path(__file__).parent / "datasets" / "retrieval" / "development" / "development_v1.jsonl"


@dataclass(frozen=True, slots=True)
class RetrievalCase:
    id: str
    scenario_type: str
    query: str
    limit: int
    filters: dict[str, Any]
    candidates: tuple[dict[str, Any], ...]
    relevant: dict[str, int]


def load_retrieval_development_cases() -> list[RetrievalCase]:
    rows = []
    for line in DATASET.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        item = json.loads(line)
        rows.append(RetrievalCase(
            id=item["id"], scenario_type=item["scenario_type"], query=item["query"],
            limit=int(item["limit"]), filters=dict(item.get("filters") or {}),
            candidates=tuple(item["candidates"]), relevant={str(k): int(v) for k, v in item["relevant"].items()},
        ))
    return rows
