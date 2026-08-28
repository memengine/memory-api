from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

QuestionType = Literal[
    "single-session-user",
    "single-session-assistant",
    "single-session-preference",
    "temporal-reasoning",
    "knowledge-update",
    "multi-session",
]


class LongMemEvalTurn(BaseModel):
    model_config = ConfigDict(extra="allow")

    role: Literal["user", "assistant"]
    # The cleaned upstream release contains a small number of blank turns.
    # Preserve them here; the API adapter drops them before ingestion.
    content: str
    has_answer: bool = False


class LongMemEvalCase(BaseModel):
    model_config = ConfigDict(extra="forbid")

    question_id: str = Field(min_length=1)
    question_type: QuestionType
    question: str = Field(min_length=1)
    answer: Any
    question_date: str = Field(min_length=1)
    haystack_session_ids: list[str]
    haystack_dates: list[str]
    haystack_sessions: list[list[LongMemEvalTurn]]
    answer_session_ids: list[str]

    @model_validator(mode="after")
    def aligned_haystack(self) -> LongMemEvalCase:
        lengths = {
            len(self.haystack_session_ids),
            len(self.haystack_dates),
            len(self.haystack_sessions),
        }
        if len(lengths) != 1:
            raise ValueError(
                "haystack ids, dates, and sessions must have equal lengths"
            )
        if not self.question_id.endswith("_abs"):
            missing = set(self.answer_session_ids) - set(self.haystack_session_ids)
            if missing:
                raise ValueError(
                    f"answer sessions missing from haystack: {sorted(missing)}"
                )
        return self

    @field_validator("haystack_sessions")
    @classmethod
    def sessions_are_not_empty(
        cls, value: list[list[LongMemEvalTurn]]
    ) -> list[list[LongMemEvalTurn]]:
        if not value or any(
            not session or not any(turn.content.strip() for turn in session)
            for session in value
        ):
            raise ValueError(
                "haystack sessions must contain at least one non-blank turn"
            )
        return value


def parse_longmemeval_datetime(value: str) -> datetime:
    # The upstream corpus does not declare a timezone. Treat its synthetic
    # timeline as UTC so runs are stable across evaluator machines.
    return datetime.strptime(value, "%Y/%m/%d (%a) %H:%M").replace(tzinfo=UTC)


def assert_public_dataset_path(path: Path) -> Path:
    resolved = path.expanduser().resolve()
    lowered = str(resolved).replace("\\", "/").lower()
    forbidden = (
        "/benchmarks/internal/",
        "/tests/evals/",
        "/holdout/",
        "holdout_v1",
    )
    if any(token in lowered for token in forbidden):
        raise ValueError(
            "public benchmark commands cannot load internal or holdout data"
        )
    return resolved


def load_dataset(path: Path) -> tuple[list[LongMemEvalCase], str]:
    resolved = assert_public_dataset_path(path)
    raw = resolved.read_bytes()
    payload = json.loads(raw)
    if not isinstance(payload, list):
        raise TypeError("LongMemEval dataset must be a JSON array")
    cases = [LongMemEvalCase.model_validate(item) for item in payload]
    ids = [case.question_id for case in cases]
    if len(ids) != len(set(ids)):
        raise ValueError("question_id values must be unique")
    return cases, hashlib.sha256(raw).hexdigest()


def select_smoke_cases(
    cases: list[LongMemEvalCase], *, count: int
) -> list[LongMemEvalCase]:
    """Select a stable wiring subset without consulting answers or labels."""
    if count < 1:
        raise ValueError("smoke count must be positive")
    return sorted(
        cases,
        key=lambda case: hashlib.sha256(case.question_id.encode("utf-8")).hexdigest(),
    )[:count]


def select_stratified_smoke_cases(
    cases: list[LongMemEvalCase],
) -> list[LongMemEvalCase]:
    """Select one stable case per question type, including one abstention."""
    ordered = sorted(
        cases,
        key=lambda case: hashlib.sha256(case.question_id.encode("utf-8")).hexdigest(),
    )
    abstention = next(
        (case for case in ordered if case.question_id.endswith("_abs")), None
    )
    if abstention is None:
        raise ValueError("stratified smoke selection requires an abstention case")
    selected = {abstention.question_type: abstention}
    for question_type in QuestionType.__args__:
        if question_type in selected:
            continue
        candidate = next(
            (case for case in ordered if case.question_type == question_type), None
        )
        if candidate is None:
            raise ValueError(f"no case available for question type {question_type}")
        selected[question_type] = candidate
    return [selected[question_type] for question_type in QuestionType.__args__]
