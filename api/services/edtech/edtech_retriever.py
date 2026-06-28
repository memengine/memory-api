from __future__ import annotations

import json
import uuid
from dataclasses import asdict
from datetime import UTC
from datetime import date
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from api.db.cache import CacheService
from api.db.models import EdTechMemory
from api.schemas.edtech_schemas import EdTechRetrieveResult
from api.services.edtech.forgetting_curve import get_review_priority

try:  # pragma: no cover
    import tiktoken
except ModuleNotFoundError:  # pragma: no cover
    tiktoken = None  # type: ignore[assignment]


class EdTechRetriever:
    CACHE_TTL_SECONDS = 300
    LEARNER_TYPE_INTROS = {
        "school_student": "What you know about this student:",
        "competitive_exam": "What you know about this aspirant:",
        "higher_education": "What you know about this student:",
        "professional_cert": "What you know about this candidate:",
        "skill_learner": "What you know about this learner:",
        "medical_student": "What you know about this medical student:",
    }

    def __init__(self, *, session: AsyncSession, cache_service: CacheService | None = None) -> None:
        self.session = session
        self.cache_service = cache_service

    async def get_for_student(
        self,
        proxy_user_id: str,
        tenant_id: str,
        query: str | None = None,
        max_tokens: int = 600,
    ) -> EdTechRetrieveResult:
        cache_key = f"edtech:{tenant_id}:{proxy_user_id}:{max_tokens}"
        if self.cache_service is not None:
            cached = await self.cache_service._get_json(cache_key)
            if cached:
                return EdTechRetrieveResult(**cached)

        result = await self.session.execute(
            select(EdTechMemory).where(
                EdTechMemory.proxy_user_id == uuid.UUID(str(proxy_user_id)),
                EdTechMemory.tenant_id == uuid.UUID(str(tenant_id)),
            )
        )
        memory = result.scalar_one_or_none()
        if memory is None:
            return EdTechRetrieveResult(system_prompt_addition="", context_token_count=0, days_to_exam=None)

        days_to_exam = _days_to_exam(memory.primary_deadline_date or memory.exam_date)
        prompt = self.build_system_prompt_addition(memory, days_to_exam=days_to_exam, max_tokens=max_tokens)
        output = EdTechRetrieveResult(
            system_prompt_addition=prompt,
            context_token_count=_token_count(prompt),
            days_to_exam=days_to_exam,
        )
        if self.cache_service is not None:
            await self.cache_service._set_json(cache_key, asdict(output), ttl=self.CACHE_TTL_SECONDS)
        return output

    def build_system_prompt_addition(
        self,
        memory: EdTechMemory,
        days_to_exam: int | None,
        max_tokens: int = 600,
    ) -> str:
        sections = [
            self.LEARNER_TYPE_INTROS.get(memory.learner_type or "school_student", "What you know about this learner:"),
            self._review_urgency_section(memory, days_to_exam),
            self._exam_section(memory, days_to_exam),
            self._extension_context_section(memory),
            self._learning_style_section(memory),
            self._strong_topics_section(memory),
            self._do_not_assume_section(memory),
        ]
        prompt = "\n\n".join(section for section in sections if section)
        return _fit_token_limit(prompt, max_tokens)

    def _review_urgency_section(self, memory: EdTechMemory, days_to_exam: int | None) -> str:
        topic_records: list[dict[str, Any]] = []
        stages = dict(memory.forgetting_stages or {})
        weak_by_topic = {
            str(item.get("topic")): item
            for item in memory.weak_topics or []
            if isinstance(item, dict) and item.get("topic")
        }
        for topic, stage_record in stages.items():
            record = dict(stage_record or {})
            record["topic"] = topic
            if topic in weak_by_topic:
                record.update({key: value for key, value in weak_by_topic[topic].items() if value is not None})
            record["priority"] = get_review_priority(record, days_to_exam)
            topic_records.append(record)

        urgent = sorted(topic_records, key=lambda item: float(item.get("priority") or 0.0), reverse=True)[:5]
        if not urgent:
            return ""
        lines = ["Review urgency:"]
        for item in urgent:
            stage = item.get("stage") or "unknown"
            days = item.get("days_since")
            gap = item.get("specific_gap")
            detail = f" - {item.get('topic')} ({stage}"
            if days is not None:
                detail += f", {days} days since review"
            detail += ")"
            if gap:
                detail += f": {gap}"
            lines.append(detail)
        return "\n".join(lines)

    def _exam_section(self, memory: EdTechMemory, days_to_exam: int | None) -> str:
        deadline_event = memory.primary_deadline_event or memory.exam_name
        if not deadline_event and days_to_exam is None and not memory.marks_target and not memory.primary_goal:
            return ""
        label = "Deadline context:" if memory.learner_type in {"skill_learner", "higher_education"} else "Exam context:"
        parts = [label]
        if memory.primary_goal:
            parts.append(f" - Goal: {memory.primary_goal}")
        if deadline_event:
            parts.append(f" - Target: {deadline_event}")
        if days_to_exam is not None:
            parts.append(f" - Countdown: {days_to_exam} days remaining")
        if memory.marks_target:
            parts.append(f" - Target: {json.dumps(memory.marks_target, ensure_ascii=True)}")
        return "\n".join(parts)

    def _extension_context_section(self, memory: EdTechMemory) -> str:
        context_by_type = {
            "competitive_exam": memory.competitive_exam_context,
            "higher_education": memory.higher_education_context,
            "professional_cert": memory.professional_cert_context,
            "skill_learner": memory.skill_learner_context,
            "medical_student": memory.medical_context,
        }
        context = context_by_type.get(memory.learner_type or "", {})
        if not context:
            return ""
        title_by_type = {
            "competitive_exam": "Competitive exam context:",
            "higher_education": "Higher education context:",
            "professional_cert": "Certification context:",
            "skill_learner": "Skill-learning context:",
            "medical_student": "Medical learning context:",
        }
        return f"{title_by_type.get(memory.learner_type or '', 'Learner context:')}\n - {json.dumps(context, ensure_ascii=True)}"

    def _learning_style_section(self, memory: EdTechMemory) -> str:
        bits = []
        if memory.explanation_style:
            bits.append(f"explanation_style={json.dumps(memory.explanation_style, ensure_ascii=True)}")
        if memory.language_profile:
            bits.append(f"language_profile={json.dumps(memory.language_profile, ensure_ascii=True)}")
        if memory.session_profile:
            bits.append(f"session_profile={json.dumps(memory.session_profile, ensure_ascii=True)}")
        if not bits:
            return ""
        return "Learning style:\n - " + "\n - ".join(bits)

    def _strong_topics_section(self, memory: EdTechMemory) -> str:
        topics = [str(item.get("topic")) for item in memory.strong_topics or [] if isinstance(item, dict) and item.get("topic")]
        if not topics:
            return ""
        return "Strong topics:\n - " + ", ".join(topics[:10])

    def _do_not_assume_section(self, memory: EdTechMemory) -> str:
        weak = [str(item.get("topic")) for item in memory.weak_topics or [] if isinstance(item, dict) and item.get("topic")]
        forgotten = [
            str(topic)
            for topic, stage in (memory.forgetting_stages or {}).items()
            if isinstance(stage, dict) and stage.get("stage") in {"critical", "forgotten"}
        ]
        combined = list(dict.fromkeys([*weak, *forgotten]))
        if not combined:
            return ""
        return "Do not assume mastery of:\n - " + ", ".join(combined[:10])


def _days_to_exam(exam_date: date | None) -> int | None:
    if exam_date is None:
        return None
    return max(0, (exam_date - datetime.now(UTC).date()).days)


def _token_count(text: str) -> int:
    if not text:
        return 0
    if tiktoken is None:
        return int(len(text.split()) * 1.3)
    return len(tiktoken.get_encoding("cl100k_base").encode(text))


def _fit_token_limit(text: str, max_tokens: int) -> str:
    if _token_count(text) <= max_tokens:
        return text
    lines = text.splitlines()
    while len(lines) > 3 and _token_count("\n".join(lines)) > max_tokens:
        lines.pop()
    return "\n".join(lines)


__all__ = ["EdTechRetriever"]
