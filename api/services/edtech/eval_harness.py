from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class EdTechEvalCase:
    id: str
    learner_type: str
    messages: list[dict[str, str]]
    expected_safe_fallback: dict[str, Any]
    expected_model_extracted: dict[str, Any]


@dataclass(frozen=True)
class EvalComparison:
    passed: bool
    missing: list[str]
    mismatched: list[str]


SEMANTIC_FIELDS = {
    "concept_gaps",
    "explanation_style",
    "language_profile",
    "last_topic_studied",
    "marks_target",
    "mock_scores",
    "primary_goal",
    "session_profile",
    "strong_topics",
    "subjects",
    "weak_topics",
}


def load_eval_cases(directory: str | Path) -> list[EdTechEvalCase]:
    root = Path(directory)
    cases: list[EdTechEvalCase] = []
    for path in sorted(root.glob("*.json")):
        raw = json.loads(path.read_text(encoding="utf-8"))
        cases.append(
            EdTechEvalCase(
                id=str(raw["id"]),
                learner_type=str(raw["learner_type"]),
                messages=list(raw["messages"]),
                expected_safe_fallback=dict(raw.get("expected_safe_fallback") or {}),
                expected_model_extracted=dict(raw.get("expected_model_extracted") or {}),
            )
        )
    return cases


def compare_expected_subset(actual: dict[str, Any], expected: dict[str, Any]) -> EvalComparison:
    missing: list[str] = []
    mismatched: list[str] = []
    _compare_node(actual, expected, path="", missing=missing, mismatched=mismatched)
    return EvalComparison(passed=not missing and not mismatched, missing=missing, mismatched=mismatched)


def _compare_node(actual: Any, expected: Any, *, path: str, missing: list[str], mismatched: list[str]) -> None:
    if isinstance(expected, dict):
        if not isinstance(actual, dict):
            mismatched.append(path or "<root>")
            return
        for key, value in expected.items():
            child_path = f"{path}.{key}" if path else str(key)
            if key not in actual:
                missing.append(child_path)
                continue
            _compare_node(actual[key], value, path=child_path, missing=missing, mismatched=mismatched)
        return

    if isinstance(expected, list):
        if not isinstance(actual, list):
            mismatched.append(path or "<root>")
            return
        for index, expected_item in enumerate(expected):
            if not _list_contains_subset(actual, expected_item):
                missing.append(f"{path}[{index}]")
        return

    if actual != expected:
        mismatched.append(path or "<root>")


def _list_contains_subset(items: list[Any], expected_item: Any) -> bool:
    if not isinstance(expected_item, dict):
        return expected_item in items
    return any(
        isinstance(item, dict)
        and compare_expected_subset(item, expected_item).passed
        for item in items
    )
