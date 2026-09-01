from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from api.services.extraction_eval_harness import load_golden_extraction_cases

ALLOWED_CATEGORIES = {"preference", "fact", "goal", "procedure", "relationship", "expertise"}
ALLOWED_DISPOSITIONS = {"store", "pending", "discard"}
ALLOWED_SPLITS = {"development", "holdout", "challenge"}
HOLDOUT_APPROVAL_ENV = "MEMORYOS_HOLDOUT_APPROVAL"
HOLDOUT_APPROVAL_TOKEN = "approved-manual-holdout-run"


@dataclass(frozen=True)
class ExpectedMemory:
    proposition: str
    category: str
    acceptable_categories: tuple[str, ...] = ()
    disposition: str = "store"
    acceptable_paraphrases: tuple[str, ...] = ()
    importance_min: float = 1.0
    importance_max: float = 10.0
    confidence_min: float = 0.0
    confidence_max: float = 1.0
    evidence_turns: tuple[int, ...] = ()
    safety_tags: tuple[str, ...] = ()


@dataclass(frozen=True)
class ExtractionCase:
    id: str
    split: str
    case_type: str
    messages: tuple[dict[str, str], ...]
    expected_memories: tuple[ExpectedMemory, ...]
    forbidden_patterns: tuple[str, ...] = ()
    tags: tuple[str, ...] = ()
    notes: str = ""
    source: str = "internal"


LEGACY_EVIDENCE_TURNS: dict[tuple[str, int], tuple[int, ...]] = {
    ("borderline_language_switch", 0): (0,),
    ("borderline_possible_goal", 0): (0,),
    ("borderline_short_today_preference", 0): (0,),
    ("composite_learning_context", 0): (0,),
    ("composite_learning_context", 1): (2,),
    ("composite_learning_context", 2): (4,),
    ("composite_role_team_goal", 0): (0,),
    ("composite_role_team_goal", 1): (2,),
    ("composite_role_team_goal", 2): (4,),
    ("composite_support_account", 0): (0,),
    ("composite_support_account", 1): (2,),
    ("composite_support_account", 2): (4,),
    ("positive_expertise_fastapi_sqlalchemy", 0): (0,),
    ("positive_expertise_fastapi_sqlalchemy", 1): (0,),
    ("positive_fact_role_company", 0): (0,),
    ("positive_fact_role_company", 1): (0,),
    ("positive_goal_data_scientist", 0): (0,),
    ("positive_preference_concise_python", 0): (0,),
    ("positive_procedure_morning_deep_work", 0): (0,),
    ("positive_relationship_manager_maya", 0): (0,),
}

LEGACY_ACCEPTABLE_CATEGORIES: dict[tuple[str, int], tuple[str, ...]] = {
    ("composite_learning_context", 1): ("fact",),
    ("composite_role_team_goal", 1): ("expertise",),
    ("composite_support_account", 1): ("expertise",),
    ("positive_fact_role_company", 1): ("expertise",),
}


def load_cases(path: str | Path, *, allow_holdout: bool = False) -> list[ExtractionCase]:
    root = Path(path)
    if "holdout" in {part.lower() for part in root.parts}:
        _require_holdout_approval(allow_holdout=allow_holdout)
    paths = sorted(root.rglob("*.jsonl")) if root.is_dir() else [root]
    cases: list[ExtractionCase] = []
    for source_path in paths:
        for line_number, line in enumerate(source_path.read_text(encoding="utf-8").splitlines(), 1):
            if not line.strip():
                continue
            raw = json.loads(line)
            if str(raw.get("split", "")).lower() == "holdout":
                _require_holdout_approval(allow_holdout=allow_holdout)
            cases.append(_parse_case(raw, source=f"{source_path}:{line_number}"))
    _validate_unique_ids(cases)
    return cases


def _require_holdout_approval(*, allow_holdout: bool) -> None:
    if not allow_holdout or os.getenv(HOLDOUT_APPROVAL_ENV) != HOLDOUT_APPROVAL_TOKEN:
        raise PermissionError(
            "Holdout access is locked. Use an explicitly approved manual command with "
            f"allow_holdout=True and {HOLDOUT_APPROVAL_ENV}={HOLDOUT_APPROVAL_TOKEN}."
        )


def load_legacy_cases(path: str | Path, *, split: str = "development") -> list[ExtractionCase]:
    cases: list[ExtractionCase] = []
    for legacy in load_golden_extraction_cases(path):
        expected = tuple(
            ExpectedMemory(
                proposition=item.content,
                category=item.category,
                acceptable_categories=LEGACY_ACCEPTABLE_CATEGORIES.get(
                    (legacy.id, index),
                    (),
                ),
                disposition="pending" if item.confidence < 0.65 else "store",
                importance_min=max(1.0, item.importance_score - 0.5),
                importance_max=min(10.0, item.importance_score + 0.5),
                confidence_min=max(0.0, item.confidence - 0.1),
                confidence_max=min(1.0, item.confidence + 0.1),
                evidence_turns=LEGACY_EVIDENCE_TURNS.get((legacy.id, index), ()),
            )
            for index, item in enumerate(legacy.expected_memories)
        )
        cases.append(
            ExtractionCase(
                id=legacy.id,
                split=split,
                case_type=legacy.case_type,
                messages=tuple(legacy.messages),
                expected_memories=expected,
                tags=("legacy",),
                notes=legacy.notes,
                source="legacy-general-extraction-cases",
            )
        )
    _validate_unique_ids(cases)
    return cases


def _parse_case(raw: dict[str, Any], *, source: str) -> ExtractionCase:
    case_id = _required_text(raw, "id", source)
    split = _required_text(raw, "split", source).lower()
    if split not in ALLOWED_SPLITS:
        raise ValueError(f"{source}: invalid split {split!r}")
    messages = raw.get("messages")
    if not isinstance(messages, list) or not messages:
        raise ValueError(f"{source}: messages must be a non-empty list")
    normalized_messages: list[dict[str, str]] = []
    for message in messages:
        if not isinstance(message, dict) or message.get("role") not in {"user", "assistant", "system"}:
            raise ValueError(f"{source}: invalid message")
        normalized_messages.append({"role": str(message["role"]), "content": _required_text(message, "content", source)})
    expected_raw = raw.get("expected_memories", [])
    if not isinstance(expected_raw, list):
        raise ValueError(f"{source}: expected_memories must be a list")
    expected = tuple(_parse_expected(item, source) for item in expected_raw)
    return ExtractionCase(
        id=case_id,
        split=split,
        case_type=_required_text(raw, "case_type", source).lower(),
        messages=tuple(normalized_messages),
        expected_memories=expected,
        forbidden_patterns=tuple(str(item).lower() for item in raw.get("forbidden_patterns", [])),
        tags=tuple(str(item) for item in raw.get("tags", [])),
        notes=str(raw.get("notes", "")),
        source=source,
    )


def _parse_expected(raw: Any, source: str) -> ExpectedMemory:
    if not isinstance(raw, dict):
        raise ValueError(f"{source}: expected memory must be an object")
    category = _required_text(raw, "category", source).lower()
    acceptable_categories = tuple(
        str(item).lower() for item in raw.get("acceptable_categories", [])
    )
    disposition = str(raw.get("disposition", "store")).lower()
    if (
        category not in ALLOWED_CATEGORIES
        or any(item not in ALLOWED_CATEGORIES for item in acceptable_categories)
        or disposition not in ALLOWED_DISPOSITIONS
    ):
        raise ValueError(f"{source}: invalid category or disposition")
    importance = raw.get("importance_range", [1.0, 10.0])
    confidence = raw.get("confidence_range", [0.0, 1.0])
    _validate_range(importance, 1.0, 10.0, "importance_range", source)
    _validate_range(confidence, 0.0, 1.0, "confidence_range", source)
    return ExpectedMemory(
        proposition=_required_text(raw, "proposition", source),
        category=category,
        acceptable_categories=acceptable_categories,
        disposition=disposition,
        acceptable_paraphrases=tuple(str(item) for item in raw.get("acceptable_paraphrases", [])),
        importance_min=float(importance[0]),
        importance_max=float(importance[1]),
        confidence_min=float(confidence[0]),
        confidence_max=float(confidence[1]),
        evidence_turns=tuple(int(item) for item in raw.get("evidence_turns", [])),
        safety_tags=tuple(str(item) for item in raw.get("safety_tags", [])),
    )


def _required_text(raw: dict[str, Any], field_name: str, source: str) -> str:
    value = str(raw.get(field_name, "")).strip()
    if not value:
        raise ValueError(f"{source}: missing {field_name}")
    return value


def _validate_range(value: Any, low: float, high: float, name: str, source: str) -> None:
    if not isinstance(value, list) or len(value) != 2:
        raise ValueError(f"{source}: {name} must contain two numbers")
    start, end = float(value[0]), float(value[1])
    if start < low or end > high or start > end:
        raise ValueError(f"{source}: invalid {name}")


def _validate_unique_ids(cases: list[ExtractionCase]) -> None:
    ids = [case.id for case in cases]
    if len(ids) != len(set(ids)):
        raise ValueError("benchmark case ids must be unique")
