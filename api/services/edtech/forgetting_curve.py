from __future__ import annotations

from datetime import date
from datetime import datetime
from typing import Any


FORGETTING_STAGES: tuple[tuple[int, int, str], ...] = (
    (0, 1, "fresh"),
    (1, 3, "at_risk"),
    (3, 7, "fading"),
    (7, 21, "critical"),
    (21, 999, "forgotten"),
)

STAGE_PRIORITY = {
    "fresh": 1.0,
    "at_risk": 3.0,
    "fading": 5.5,
    "critical": 8.0,
    "forgotten": 9.0,
}

SEVERITY_PRIORITY = {
    "mild": 1.0,
    "moderate": 2.0,
    "severe": 3.0,
}


def compute_forgetting_stage(days_since_review: int) -> str:
    days = max(0, int(days_since_review))
    for start, end, stage in FORGETTING_STAGES:
        if start <= days < end:
            return stage
    return "forgotten"


def _parse_date(value: Any) -> date | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def days_since(value: Any, *, today: date | None = None) -> int | None:
    parsed = _parse_date(value)
    if parsed is None:
        return None
    reference = today or date.today()
    return max(0, (reference - parsed).days)


def get_review_priority(topic_record: dict[str, Any], days_to_exam: int | None) -> float:
    """Score a topic's review urgency on a 0-10 scale.

    The score intentionally uses stable signals available in the schema:
    forgetting stage, weak-topic severity, days since review, and exam proximity.
    """
    stage = str(topic_record.get("stage") or topic_record.get("forgetting_stage") or "").lower()
    if not stage:
        days = int(topic_record.get("days_since") or topic_record.get("days_since_review") or 0)
        stage = compute_forgetting_stage(days)

    score = STAGE_PRIORITY.get(stage, 4.0)
    severity = str(topic_record.get("severity") or "").lower()
    score += SEVERITY_PRIORITY.get(severity, 0.0)

    confidence = topic_record.get("confidence")
    if confidence is not None:
        try:
            score += max(0.0, 1.0 - float(confidence)) * 1.5
        except (TypeError, ValueError):
            pass

    attempts = topic_record.get("attempts")
    if attempts is not None:
        try:
            score += min(1.0, int(attempts) / 5)
        except (TypeError, ValueError):
            pass

    if days_to_exam is not None:
        if days_to_exam <= 7:
            score += 1.5
        elif days_to_exam <= 30:
            score += 0.8

    return round(max(0.0, min(10.0, score)), 2)


__all__ = [
    "FORGETTING_STAGES",
    "compute_forgetting_stage",
    "days_since",
    "get_review_priority",
]
