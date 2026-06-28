from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any


ALLOWED_GOLDEN_CASE_TYPES = {
    "positive",
    "negative",
    "composite",
    "borderline",
}

ALLOWED_GOLDEN_CATEGORIES = {
    "preference",
    "fact",
    "goal",
    "procedure",
    "relationship",
    "expertise",
}


@dataclass(frozen=True)
class GoldenExpectedMemory:
    content: str
    category: str
    confidence: float
    importance_score: float


@dataclass(frozen=True)
class GoldenExtractionCase:
    id: str
    case_type: str
    messages: list[dict[str, str]]
    expected_memories: list[GoldenExpectedMemory]
    expected_nothing_to_extract: bool
    notes: str


@dataclass(frozen=True)
class GoldenComparison:
    passed: bool
    missing: list[str]
    unexpected: list[str]
    mismatched: list[str]


def load_golden_extraction_cases(directory: str | Path) -> list[GoldenExtractionCase]:
    root = Path(directory)
    cases: list[GoldenExtractionCase] = []
    for path in sorted(root.glob("*.json")):
        raw = json.loads(path.read_text(encoding="utf-8"))
        cases.append(_parse_case(raw, source_path=path))
    return cases


def compare_expected_memories(
    actual: list[dict[str, Any]],
    expected: list[GoldenExpectedMemory],
) -> GoldenComparison:
    actual_by_content = {
        _normalize_text(str(item.get("content") or "")): item
        for item in actual
        if str(item.get("content") or "").strip()
    }
    missing: list[str] = []
    mismatched: list[str] = []

    for expected_item in expected:
        key = _normalize_text(expected_item.content)
        actual_item = actual_by_content.get(key)
        if actual_item is None:
            missing.append(expected_item.content)
            continue
        if str(actual_item.get("category") or "").lower() != expected_item.category:
            mismatched.append(f"{expected_item.content}: category")

    expected_keys = {_normalize_text(item.content) for item in expected}
    unexpected = [
        str(item.get("content"))
        for key, item in actual_by_content.items()
        if key not in expected_keys
    ]
    return GoldenComparison(
        passed=not missing and not unexpected and not mismatched,
        missing=missing,
        unexpected=unexpected,
        mismatched=mismatched,
    )


def _parse_case(raw: dict[str, Any], *, source_path: Path) -> GoldenExtractionCase:
    case_id = str(raw.get("id") or "").strip()
    if not case_id:
        raise ValueError(f"{source_path}: missing id")

    case_type = str(raw.get("case_type") or "").strip().lower()
    if case_type not in ALLOWED_GOLDEN_CASE_TYPES:
        raise ValueError(f"{source_path}: invalid case_type {case_type!r}")

    messages = raw.get("messages")
    if not isinstance(messages, list) or not messages:
        raise ValueError(f"{source_path}: messages must be a non-empty list")
    normalized_messages = [_parse_message(item, source_path=source_path) for item in messages]

    expected_raw = raw.get("expected_memories") or []
    if not isinstance(expected_raw, list):
        raise ValueError(f"{source_path}: expected_memories must be a list")
    expected = [_parse_expected_memory(item, source_path=source_path) for item in expected_raw]

    expected_nothing = bool(raw.get("expected_nothing_to_extract", False))
    if expected_nothing and expected:
        raise ValueError(f"{source_path}: nothing_to_extract cases cannot expect memories")
    if not expected_nothing and not expected:
        raise ValueError(f"{source_path}: positive cases must expect at least one memory")

    return GoldenExtractionCase(
        id=case_id,
        case_type=case_type,
        messages=normalized_messages,
        expected_memories=expected,
        expected_nothing_to_extract=expected_nothing,
        notes=str(raw.get("notes") or "").strip(),
    )


def _parse_message(raw: Any, *, source_path: Path) -> dict[str, str]:
    if not isinstance(raw, dict):
        raise ValueError(f"{source_path}: message must be an object")
    role = str(raw.get("role") or "").strip().lower()
    content = str(raw.get("content") or "").strip()
    if role not in {"user", "assistant", "system"}:
        raise ValueError(f"{source_path}: invalid message role {role!r}")
    if not content:
        raise ValueError(f"{source_path}: message content cannot be empty")
    return {"role": role, "content": content}


def _parse_expected_memory(raw: Any, *, source_path: Path) -> GoldenExpectedMemory:
    if not isinstance(raw, dict):
        raise ValueError(f"{source_path}: expected memory must be an object")
    content = str(raw.get("content") or "").strip()
    category = str(raw.get("category") or "").strip().lower()
    if len(content) < 10:
        raise ValueError(f"{source_path}: expected memory content is too short")
    if category not in ALLOWED_GOLDEN_CATEGORIES:
        raise ValueError(f"{source_path}: invalid expected category {category!r}")
    confidence = _bounded_float(raw.get("confidence"), low=0.0, high=1.0, field="confidence", source_path=source_path)
    importance = _bounded_float(
        raw.get("importance_score"),
        low=1.0,
        high=10.0,
        field="importance_score",
        source_path=source_path,
    )
    return GoldenExpectedMemory(
        content=content,
        category=category,
        confidence=confidence,
        importance_score=importance,
    )


def _bounded_float(
    value: Any,
    *,
    low: float,
    high: float,
    field: str,
    source_path: Path,
) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{source_path}: {field} must be numeric") from exc
    if parsed < low or parsed > high:
        raise ValueError(f"{source_path}: {field} must be between {low} and {high}")
    return parsed


def _normalize_text(value: str) -> str:
    return " ".join(value.strip().lower().rstrip(".?!").split())
