from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

LoCoMoCategory = Literal[1, 2, 3, 4, 5]

_SESSION_KEY = re.compile(r"^session_(\d+)$")
_DIALOG_REFERENCE = re.compile(r"D\d+:\d+")


class LoCoMoTurn(BaseModel):
    model_config = ConfigDict(extra="allow")

    speaker: str = Field(min_length=1)
    dia_id: str = Field(pattern=r"^D\d+:\d+$")
    text: str = Field(min_length=1)
    img_url: str | None = None
    blip_caption: str | None = None


class LoCoMoQuestion(BaseModel):
    model_config = ConfigDict(extra="forbid")

    question: str = Field(min_length=1)
    category: LoCoMoCategory
    evidence: list[str] = Field(default_factory=list)
    answer: Any | None = None
    adversarial_answer: Any | None = None

    @field_validator("evidence")
    @classmethod
    def evidence_is_nonblank(cls, value: list[str]) -> list[str]:
        if any(not item.strip() for item in value):
            raise ValueError("evidence references must not be blank")
        return value

    @model_validator(mode="after")
    def category_has_expected_answer(self) -> LoCoMoQuestion:
        if self.category == 5:
            if self.adversarial_answer is None:
                raise ValueError("category 5 requires adversarial_answer")
        elif self.answer is None:
            raise ValueError("categories 1-4 require answer")
        return self

    def evidence_dialog_ids(self) -> tuple[str, ...]:
        """Return atomic dialog IDs while preserving the upstream evidence field."""
        found: list[str] = []
        for value in self.evidence:
            for dialog_id in _DIALOG_REFERENCE.findall(value):
                if dialog_id not in found:
                    found.append(dialog_id)
        return tuple(found)


class LoCoMoConversation(BaseModel):
    model_config = ConfigDict(extra="allow")

    speaker_a: str = Field(min_length=1)
    speaker_b: str = Field(min_length=1)

    @model_validator(mode="after")
    def sessions_are_well_formed(self) -> LoCoMoConversation:
        sessions = self.sessions()
        if not sessions:
            raise ValueError("conversation must contain at least one session")
        speakers = {self.speaker_a, self.speaker_b}
        seen_dialog_ids: set[str] = set()
        for number, _timestamp, turns in sessions:
            if not turns:
                raise ValueError(f"session_{number} must not be empty")
            for turn in turns:
                if turn.speaker not in speakers:
                    raise ValueError(
                        f"dialog {turn.dia_id} uses unknown speaker {turn.speaker!r}"
                    )
                if turn.dia_id in seen_dialog_ids:
                    raise ValueError(f"duplicate dialog ID: {turn.dia_id}")
                seen_dialog_ids.add(turn.dia_id)
        return self

    def sessions(self) -> list[tuple[int, datetime, list[LoCoMoTurn]]]:
        extra = self.model_extra or {}
        numbered: list[tuple[int, datetime, list[LoCoMoTurn]]] = []
        for key, raw_turns in extra.items():
            match = _SESSION_KEY.fullmatch(key)
            if match is None:
                continue
            number = int(match.group(1))
            timestamp_key = f"session_{number}_date_time"
            raw_timestamp = extra.get(timestamp_key)
            if not isinstance(raw_timestamp, str):
                raise TypeError(f"{key} is missing {timestamp_key}")
            if not isinstance(raw_turns, list):
                raise TypeError(f"{key} must be a list")
            turns = [LoCoMoTurn.model_validate(item) for item in raw_turns]
            numbered.append((number, parse_locomo_datetime(raw_timestamp), turns))
        numbered.sort(key=lambda item: item[0])
        expected = list(range(1, len(numbered) + 1))
        actual = [item[0] for item in numbered]
        if actual != expected:
            raise ValueError(f"session numbering must be contiguous: {actual}")
        return numbered

    def dialog_ids(self) -> set[str]:
        return {
            turn.dia_id
            for _number, _timestamp, turns in self.sessions()
            for turn in turns
        }


class LoCoMoSample(BaseModel):
    model_config = ConfigDict(extra="allow")

    sample_id: str = Field(min_length=1)
    conversation: LoCoMoConversation
    qa: list[LoCoMoQuestion] = Field(min_length=1)

    def question_id(self, index: int) -> str:
        if index < 0 or index >= len(self.qa):
            raise IndexError(index)
        return f"{self.sample_id}:qa-{index}"


def parse_locomo_datetime(value: str) -> datetime:
    # The synthetic corpus has no timezone declaration. UTC keeps runs stable.
    return datetime.strptime(value.strip(), "%I:%M %p on %d %B, %Y").replace(tzinfo=UTC)


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


def load_dataset(path: Path) -> tuple[list[LoCoMoSample], str]:
    resolved = assert_public_dataset_path(path)
    raw = resolved.read_bytes()
    payload = json.loads(raw)
    if not isinstance(payload, list):
        raise TypeError("LoCoMo dataset must be a JSON array")
    samples = [LoCoMoSample.model_validate(item) for item in payload]
    sample_ids = [sample.sample_id for sample in samples]
    if len(sample_ids) != len(set(sample_ids)):
        raise ValueError("sample_id values must be unique")
    return samples, hashlib.sha256(raw).hexdigest()


def dataset_diagnostics(samples: list[LoCoMoSample]) -> dict[str, Any]:
    category_counts = {str(category): 0 for category in range(1, 6)}
    unresolved: list[dict[str, str]] = []
    session_count = 0
    turn_count = 0
    for sample in samples:
        sessions = sample.conversation.sessions()
        session_count += len(sessions)
        turn_count += sum(len(turns) for _number, _timestamp, turns in sessions)
        dialog_ids = sample.conversation.dialog_ids()
        for index, question in enumerate(sample.qa):
            category_counts[str(question.category)] += 1
            for evidence_id in question.evidence_dialog_ids():
                if evidence_id not in dialog_ids:
                    unresolved.append(
                        {
                            "question_id": sample.question_id(index),
                            "evidence_dialog_id": evidence_id,
                        }
                    )
    return {
        "sample_count": len(samples),
        "session_count": session_count,
        "turn_count": turn_count,
        "question_count": sum(category_counts.values()),
        "category_counts": category_counts,
        "unresolved_evidence_count": len(unresolved),
        "unresolved_evidence": unresolved,
    }
