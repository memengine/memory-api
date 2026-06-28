from __future__ import annotations

import re
from typing import Any

from api.db.models import EdTechMemory
from api.services.domain_projection_types import DomainMemoryProjection


def build_edtech_universal_projections(memory: EdTechMemory) -> list[DomainMemoryProjection]:
    """Build portable cross-agent memories from the structured EdTech profile.

    This intentionally projects only low-risk facts that help other learning
    agents personalize. The full EdTech state remains in edtech_memories.
    """

    record_id = str(memory.id)
    projections: list[DomainMemoryProjection] = []

    learner_type = getattr(memory, "learner_type", None)
    if learner_type:
        projections.append(
            _projection(
                record_id,
                "learner_profile.learner_type",
                f"Learner type is {learner_type.replace('_', ' ')}.",
                "fact",
                8.0,
            )
        )

    primary_goal = getattr(memory, "primary_goal", None)
    if primary_goal:
        projections.append(
            _projection(
                record_id,
                "learner_profile.primary_goal",
                f"Learning goal: {primary_goal}.",
                "goal",
                8.5,
            )
        )

    grade_level = getattr(memory, "grade_level", None)
    if grade_level:
        projections.append(
            _projection(
                record_id,
                "academic_profile.grade_level",
                f"Student is in {grade_level}.",
                "fact",
                7.0,
            )
        )

    board_or_curriculum = getattr(memory, "board_or_curriculum", None)
    if board_or_curriculum:
        projections.append(
            _projection(
                record_id,
                "academic_profile.board_or_curriculum",
                f"Student follows {board_or_curriculum}.",
                "fact",
                6.5,
            )
        )

    deadline_event = getattr(memory, "primary_deadline_event", None) or getattr(memory, "exam_name", None)
    if deadline_event:
        projections.append(
            _projection(
                record_id,
                "deadline_context.primary_event",
                f"Student is preparing for {deadline_event}.",
                "goal",
                8.0,
            )
        )

    explanation_style = getattr(memory, "explanation_style", None) or {}
    primary_style = _string_value(explanation_style.get("primary"))
    if primary_style:
        projections.append(
            _projection(
                record_id,
                "learning_behaviour.explanation_style.primary",
                f"Student learns best through {primary_style}.",
                "preference",
                7.5,
            )
        )

    language_profile = getattr(memory, "language_profile", None) or {}
    language_preference = _string_value(
        language_profile.get("explanation_preference")
        or language_profile.get("primary")
        or language_profile.get("comfort")
    )
    if language_preference:
        projections.append(
            _projection(
                record_id,
                "learning_behaviour.language_profile",
                f"Student prefers learning explanations in {language_preference}.",
                "preference",
                7.0,
            )
        )

    for topic_record in _top_topic_records(getattr(memory, "strong_topics", None), limit=3):
        topic = _topic_name(topic_record)
        if not topic:
            continue
        projections.append(
            _projection(
                record_id,
                f"knowledge_state.strong_topic.{_slug(topic)}",
                f"Student is strong in {topic}.",
                "expertise",
                6.5,
                confidence=float(topic_record.get("confidence") or 0.75),
            )
        )

    for topic_record in _top_topic_records(getattr(memory, "weak_topics", None), limit=3):
        topic = _topic_name(topic_record)
        if not topic:
            continue
        specific_gap = _string_value(topic_record.get("specific_gap") or topic_record.get("still_confused"))
        gap_text = f": {specific_gap}" if specific_gap else ""
        projections.append(
            _projection(
                record_id,
                f"knowledge_state.weak_topic.{_slug(topic)}",
                f"Student is working on improving {topic}{gap_text}.",
                "expertise",
                _weak_topic_importance(topic_record),
                confidence=0.75,
            )
        )

    return projections


def _projection(
    record_id: str,
    source_field: str,
    content: str,
    category: str,
    importance_score: float,
    *,
    confidence: float = 0.8,
) -> DomainMemoryProjection:
    return DomainMemoryProjection(
        projection_key=f"edtech:{record_id}:{source_field}",
        content=content,
        category=category,
        importance_score=importance_score,
        confidence=confidence,
        source_domain="edtech",
        source_domain_record_id=record_id,
        source_field=source_field,
        portability="cross_agent_learning",
        sensitivity="normal",
    )


def _string_value(value: Any) -> str | None:
    if value is None:
        return None
    text = " ".join(str(value).split())
    return text or None


def _topic_name(topic_record: Any) -> str | None:
    if not isinstance(topic_record, dict):
        return None
    return _string_value(topic_record.get("topic") or topic_record.get("concept") or topic_record.get("name"))


def _top_topic_records(records: Any, *, limit: int) -> list[dict[str, Any]]:
    if not isinstance(records, list):
        return []
    dict_records = [record for record in records if isinstance(record, dict)]
    return dict_records[:limit]


def _weak_topic_importance(topic_record: dict[str, Any]) -> float:
    severity = str(topic_record.get("severity") or "").lower()
    if severity == "severe":
        return 8.0
    if severity == "moderate":
        return 7.0
    return 6.0


def _slug(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return slug or "unknown"


__all__ = ["build_edtech_universal_projections"]
