from __future__ import annotations

import asyncio
import json
import logging
import re
import uuid
from datetime import UTC
from datetime import date
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from api.db.models import AuditAction
from api.db.models import AuditLog
from api.db.models import EdTechMemory
from api.schemas.edtech_schemas import EdTechExtractionResult
from api.services.edtech.forgetting_curve import compute_forgetting_stage
from api.services.edtech.prompt_builder import EdTechPromptBuilder
from api.services.llm_service import LLMProvider
from api.services.llm_service import LLMService

try:  # pragma: no cover - dependency exists in production, tests can run without it.
    import tiktoken
except ModuleNotFoundError:  # pragma: no cover
    tiktoken = None  # type: ignore[assignment]


LOGGER = logging.getLogger(__name__)
MAX_CONVERSATION_TOKENS = 4000
ARRAY_FIELDS = {"subjects", "strong_topics", "weak_topics", "concept_gaps", "misconceptions", "mock_scores"}
DICT_FIELDS = {"syllabus_stage", "explanation_style", "session_profile", "language_profile", "peak_hours", "marks_target", "streak"}
SCALAR_FIELDS = {"grade_level", "board_or_curriculum", "exam_name", "exam_date", "last_topic_studied"}
ALL_EXTRACTABLE_FIELDS = ARRAY_FIELDS | DICT_FIELDS | SCALAR_FIELDS


class EdTechExtractionError(RuntimeError):
    pass


class EdTechExtractor:
    def __init__(
        self,
        *,
        session: Session,
        llm_service: LLMService | None = None,
        prompt_builder: EdTechPromptBuilder | None = None,
        client: Any | None = None,
    ) -> None:
        self.session = session
        self.llm_service = llm_service or LLMService(
            provider_clients={LLMProvider.GEMINI: client} if client is not None else None,
            require_provider=client is None,
            use_state_store=client is None,
        )
        self.prompt_builder = prompt_builder or EdTechPromptBuilder()

    def extract_and_merge_sync(
        self,
        *,
        messages: list[dict[str, Any]],
        proxy_user_id: str,
        tenant_id: str,
        job_id: str,
    ) -> EdTechExtractionResult:
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(
                self.extract_and_merge(
                    messages=messages,
                    proxy_user_id=proxy_user_id,
                    tenant_id=tenant_id,
                    job_id=job_id,
                )
            )
        raise RuntimeError("extract_and_merge_sync cannot be called from a running event loop.")

    async def extract_and_merge(
        self,
        messages: list[dict[str, Any]],
        proxy_user_id: str,
        tenant_id: str,
        job_id: str,
    ) -> EdTechExtractionResult:
        tenant_uuid = uuid.UUID(str(tenant_id))
        proxy_uuid = uuid.UUID(str(proxy_user_id))
        existing = self.session.execute(
            select(EdTechMemory).where(
                EdTechMemory.proxy_user_id == proxy_uuid,
                EdTechMemory.tenant_id == tenant_uuid,
            )
        ).scalar_one_or_none()

        conversation = self._build_conversation(messages)
        compressed = self.prompt_builder.compress_existing_memory(existing)
        prompt = self.prompt_builder.build_prompt(
            conversation=conversation,
            existing_memory_compressed=compressed,
            is_first_interaction=existing is None,
        )
        fallback_extracted = self._fallback_extract_from_user_text(messages)
        tokens_used = 0
        provider_used = "deterministic_fallback"
        try:
            response = await self.llm_service.complete(
                system_prompt=prompt,
                user_message="Extract education memory from the conversation above.",
                temperature=0.0,
                max_tokens=800,
                response_format="json",
            )
            tokens_used = int(response.total_tokens or 0)
            provider_used = response.provider_used
            data = self._parse_response(response.content)
        except Exception as exc:
            if not fallback_extracted:
                raise
            LOGGER.warning(
                "edtech_llm_failed_using_deterministic_fallback",
                extra={
                    "event": "edtech_llm_failed_using_deterministic_fallback",
                    "tenant_id": tenant_id,
                    "proxy_user_id": proxy_user_id,
                    "job_id": job_id,
                    "error": str(exc),
                },
            )
            data = {"nothing_to_extract": False, "extracted": {}, "conflicts": []}
        extracted = data.get("extracted") or {}
        if extracted and not isinstance(extracted, dict):
            raise EdTechExtractionError("EdTech extraction response field 'extracted' must be an object.")

        extracted = _merge_extracted_payload(fallback_extracted, extracted)
        if data.get("nothing_to_extract") and not extracted:
            return EdTechExtractionResult(
                fields_updated=[],
                conflicts_resolved=0,
                nothing_to_extract=True,
                tokens_used=tokens_used,
                provider_used=provider_used,
            )

        memory = existing or EdTechMemory(
            id=uuid.uuid4(),
            proxy_user_id=proxy_uuid,
            tenant_id=tenant_uuid,
        )
        fields_updated: set[str] = set()
        conflicts_resolved = self._apply_conflicts(memory, data.get("conflicts") or [], fields_updated)
        self._merge_extracted(memory, extracted, fields_updated)
        self._update_forgetting_stages(memory, extracted)

        memory.last_extraction_at = datetime.now(UTC)
        memory.updated_at = datetime.now(UTC)
        existing_job_ids = list(memory.extraction_source_job_ids or [])
        try:
            parsed_job_id = uuid.UUID(str(job_id))
            if parsed_job_id not in existing_job_ids:
                existing_job_ids.append(parsed_job_id)
        except (TypeError, ValueError):
            pass
        memory.extraction_source_job_ids = existing_job_ids
        self._upsert_memory(memory)

        return EdTechExtractionResult(
            fields_updated=sorted(fields_updated),
            conflicts_resolved=conflicts_resolved,
            nothing_to_extract=False,
            tokens_used=tokens_used,
            provider_used=provider_used,
        )

    def _upsert_memory(self, memory: EdTechMemory) -> None:
        values = {
            "id": memory.id,
            "proxy_user_id": memory.proxy_user_id,
            "tenant_id": memory.tenant_id,
            "grade_level": memory.grade_level,
            "board_or_curriculum": memory.board_or_curriculum,
            "subjects": memory.subjects or [],
            "syllabus_stage": memory.syllabus_stage or {},
            "strong_topics": memory.strong_topics or [],
            "weak_topics": memory.weak_topics or [],
            "concept_gaps": memory.concept_gaps or [],
            "misconceptions": memory.misconceptions or [],
            "explanation_style": memory.explanation_style,
            "session_profile": memory.session_profile,
            "language_profile": memory.language_profile,
            "peak_hours": memory.peak_hours,
            "exam_name": memory.exam_name,
            "exam_date": memory.exam_date,
            "marks_target": memory.marks_target,
            "mock_scores": memory.mock_scores or [],
            "forgetting_stages": memory.forgetting_stages or {},
            "improvement_velocity": memory.improvement_velocity or {},
            "streak": memory.streak,
            "last_topic_studied": memory.last_topic_studied,
            "schema_version": memory.schema_version or 1,
            "last_extraction_at": memory.last_extraction_at,
            "extraction_source_job_ids": memory.extraction_source_job_ids or [],
            "updated_at": datetime.now(UTC),
        }
        insert_stmt = pg_insert(EdTechMemory).values(**values)
        update_values = {
            key: getattr(insert_stmt.excluded, key)
            for key in values
            if key not in {"id", "proxy_user_id", "tenant_id"}
        }
        self.session.execute(
            insert_stmt.on_conflict_do_update(
                index_elements=["proxy_user_id", "tenant_id"],
                set_=update_values,
            )
        )
        self.session.flush()

    def _parse_response(self, raw: str) -> dict[str, Any]:
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            LOGGER.warning("edtech_invalid_json", extra={"event": "edtech_invalid_json", "raw_response": raw[:1000]})
            raise EdTechExtractionError("EdTech extractor returned invalid JSON.") from exc
        if not isinstance(data, dict):
            raise EdTechExtractionError("EdTech extraction response must be a JSON object.")
        return data

    def _apply_conflicts(self, memory: EdTechMemory, conflicts: list[Any], fields_updated: set[str]) -> int:
        resolved = 0
        for conflict in conflicts:
            if not isinstance(conflict, dict):
                continue
            field = str(conflict.get("field") or "")
            if field not in ALL_EXTRACTABLE_FIELDS:
                continue
            resolution = str(conflict.get("resolution") or "").lower()
            if resolution == "update":
                self._apply_field(memory, field, conflict.get("new_value"), fields_updated)
                resolved += 1
            elif resolution == "clear":
                setattr(memory, field, [] if field in ARRAY_FIELDS else ({} if field == "syllabus_stage" else None))
                fields_updated.add(field)
                resolved += 1
            if resolution in {"update", "clear"}:
                self.session.add(
                    AuditLog(
                        proxy_user_id=memory.proxy_user_id,
                        action=AuditAction.updated,
                        old_value={"field": field, "existing_value": conflict.get("existing_value")},
                        new_value={"field": field, "new_value": conflict.get("new_value"), "resolution": resolution},
                        metadata_json={
                            "event": "edtech_conflict_resolved",
                            "reason": conflict.get("reason"),
                        },
                    )
                )
        return resolved

    def _merge_extracted(self, memory: EdTechMemory, extracted: dict[str, Any], fields_updated: set[str]) -> None:
        for field, value in extracted.items():
            if field not in ALL_EXTRACTABLE_FIELDS or value in (None, "", [], {}):
                continue
            self._apply_field(memory, field, value, fields_updated)

    def _apply_field(self, memory: EdTechMemory, field: str, value: Any, fields_updated: set[str]) -> None:
        if field in SCALAR_FIELDS:
            if field == "exam_date":
                value = _parse_iso_date(value)
                if value is None:
                    return
            setattr(memory, field, str(value) if field != "exam_date" else value)
            fields_updated.add(field)
            return

        if field in ARRAY_FIELDS:
            current = list(getattr(memory, field) or [])
            if isinstance(value, dict):
                value = [value]
            if not isinstance(value, list):
                return
            setattr(memory, field, _merge_list_by_key(current, value, _merge_key_for(field)))
            fields_updated.add(field)
            return

        if field in DICT_FIELDS:
            current = dict(getattr(memory, field) or {})
            if not isinstance(value, dict):
                return
            current.update(value)
            setattr(memory, field, current)
            fields_updated.add(field)

    def _update_forgetting_stages(self, memory: EdTechMemory, extracted: dict[str, Any]) -> None:
        stages = dict(memory.forgetting_stages or {})
        today = date.today().isoformat()
        touched_topics = set()
        for field in ("strong_topics", "weak_topics"):
            value = extracted.get(field)
            if isinstance(value, list):
                for item in value:
                    if isinstance(item, dict) and item.get("topic"):
                        touched_topics.add(str(item["topic"]))
        if memory.last_topic_studied and not _is_low_value_topic(str(memory.last_topic_studied)):
            touched_topics.add(str(memory.last_topic_studied))

        known_topics = set(touched_topics)
        for item in list(memory.strong_topics or []) + list(memory.weak_topics or []):
            if isinstance(item, dict) and item.get("topic"):
                known_topics.add(str(item["topic"]))

        for topic in known_topics:
            current = dict(stages.get(topic) or {})
            if topic in touched_topics:
                current.update({"stage": "fresh", "last_reviewed": today, "days_since": 0, "review_due": today})
            else:
                last_reviewed = _parse_iso_date(current.get("last_reviewed"))
                days = (date.today() - last_reviewed).days if last_reviewed else 999
                current.update({"stage": compute_forgetting_stage(days), "days_since": max(0, days)})
            stages[topic] = current
        memory.forgetting_stages = stages

    def _build_conversation(self, messages: list[dict[str, Any]]) -> str:
        lines = []
        for message in messages:
            role = str(message.get("role") or "user").lower()
            content = str(message.get("content") or "").strip()
            if content:
                lines.append(f"[{role}]: {content}")
        conversation = "\n".join(lines)
        return _truncate_tokens(conversation, MAX_CONVERSATION_TOKENS)

    def _fallback_extract_from_user_text(self, messages: list[dict[str, Any]]) -> dict[str, Any]:
        """Conservative deterministic extraction for obvious EdTech signals."""
        user_text = ". ".join(
            str(message.get("content") or "").strip()
            for message in messages
            if str(message.get("role") or "user").lower() == "user"
        )
        user_messages = [
            str(message.get("content") or "").strip()
            for message in messages
            if str(message.get("role") or "user").lower() == "user"
        ]
        lower = user_text.lower()
        extracted: dict[str, Any] = {}

        grade_level = _extract_grade_level(lower)
        if grade_level:
            extracted["grade_level"] = grade_level

        board_or_curriculum = _extract_board_or_curriculum(lower)
        if board_or_curriculum:
            extracted["board_or_curriculum"] = board_or_curriculum

        exam_name = _extract_exam_name(lower)
        if exam_name:
            extracted["exam_name"] = exam_name

        subjects = _extract_subjects(lower)
        if subjects:
            extracted["subjects"] = subjects

        weak_topics = _extract_weak_topics(lower)
        if weak_topics:
            extracted["weak_topics"] = weak_topics

        last_topic = _extract_last_topic_studied(user_messages)
        if last_topic:
            extracted["last_topic_studied"] = last_topic

        language_profile = _extract_language_profile(lower)
        if language_profile:
            extracted["language_profile"] = language_profile

        return extracted


def _merge_key_for(field: str) -> str:
    return {
        "subjects": "subject",
        "concept_gaps": "concept",
        "misconceptions": "belief",
        "mock_scores": "date",
    }.get(field, "topic")


def _merge_extracted_payload(fallback: dict[str, Any], model_extracted: dict[str, Any]) -> dict[str, Any]:
    merged = dict(fallback or {})
    for field, value in (model_extracted or {}).items():
        if value in (None, "", [], {}):
            continue
        if field in ARRAY_FIELDS:
            current = list(merged.get(field) or [])
            incoming = value if isinstance(value, list) else [value]
            merged[field] = _merge_list_by_key(current, incoming, _merge_key_for(field))
        elif field in DICT_FIELDS and isinstance(value, dict):
            merged[field] = {**dict(merged.get(field) or {}), **value}
        else:
            merged[field] = value
    return merged


def _extract_grade_level(lower: str) -> str | None:
    match = re.search(r"\b(?:class|grade)\s*[-:]?\s*(\d{1,2})\b", lower)
    if not match:
        match = re.search(r"\b(\d{1,2})(?:st|nd|rd|th)?\s+(?:class|grade)\b", lower)
    if not match:
        return None
    grade = int(match.group(1))
    if 1 <= grade <= 12:
        return f"Class {grade}"
    return None


def _extract_board_or_curriculum(lower: str) -> str | None:
    patterns = (
        r"\b(?:my|our|school|college)?\s*(?:board|curriculum)\s+(?:is|based on|from)\s+([^.,;]+)",
        r"\b(?:i am|i'm|im)\s+(?:in|from)\s+([^.,;]+?)\s+(?:board|curriculum)\b",
    )
    for pattern in patterns:
        match = re.search(pattern, lower)
        if not match:
            continue
        raw_candidate = match.group(1)
        if _looks_like_schedule_fragment(raw_candidate):
            continue
        candidate = _clean_topic(_strip_time_context(raw_candidate))
        if candidate:
            return candidate
    return None


def _extract_exam_name(lower: str) -> str | None:
    candidates: list[str] = []
    patterns = (
        r"\b(?:preparing for|target(?:ing)?|crack(?:ing)?|appearing for|focusing on)\s+(.{2,80}?\bexam(?:s)?\b)",
        r"\b(?:my|our|the)\s+(.{2,80}?\bexam(?:s)?\b)\s+(?:is|are)\b",
        r"\bexam(?:s)?\s+(?:is|are|called|named)\s+([^.;]+)",
    )
    for pattern in patterns:
        for match in re.finditer(pattern, lower):
            candidates.extend(_split_learning_list(match.group(1)))

    cleaned = []
    for candidate in candidates:
        topic = _clean_exam_name(candidate)
        if topic:
            cleaned.append(topic)
    return " + ".join(_dedupe_strings(cleaned)[:5]) if cleaned else None


def _extract_subjects(lower: str) -> list[dict[str, Any]]:
    candidates: list[str] = []
    list_patterns = (
        r"\b(?:subjects?|topics?)\s+(?:are|is)\s+([^.;]+)",
        r"\b(?:studying|study|covering|preparing for)\s+([^.;]+)",
        r"\b(?:focus|focusing)\s+on\s+([^.;]+)",
    )
    for pattern in list_patterns:
        for match in re.finditer(pattern, lower):
            candidates.extend(_split_learning_list(match.group(1)))

    subjects = []
    for candidate in _dedupe_strings(candidates):
        subject = _clean_topic(candidate)
        if not subject:
            continue
        subjects.append(
            {
                "subject": subject,
                "confidence": 3,
                "priority": "medium",
                "note": "Mentioned by the student.",
            }
        )
    return subjects[:8]


def _extract_weak_topics(lower: str) -> list[dict[str, Any]]:
    weak_topics: list[dict[str, Any]] = []
    patterns = (
        r"\bproblem\s+(?:in|with|on)\s+([^.,;]+)",
        r"\b(?:stuck|confused|weak|struggling|difficulty|difficulties)\s+(?:everytime\s+)?(?:in|with|on)\s+([^.,;]+)",
        r"\b([^.,;]+?)\s+(?:is|feels|looks)\s+(?:hard|difficult|confusing|impossible)\b",
        r"\b(?:hard|difficult)\s+(?:topic|subject)\s+(?:is\s+)?([^.,;]+)",
    )
    for pattern in patterns:
        for match in re.finditer(pattern, lower):
            raw_topic = _clean_topic(match.group(1))
            if not raw_topic:
                continue
            weak_topics.append(
                {
                    "topic": raw_topic,
                    "severity": "moderate",
                    "attempts": 1,
                    "specific_gap": "Student explicitly described difficulty with this topic or subject.",
                    "evidence": "Detected from the student's own statement.",
                }
            )
    return _dedupe_topic_records(weak_topics)


def _extract_last_topic_studied(user_messages: list[str]) -> str | None:
    candidates: list[str] = []
    explicit_patterns = (
        r"\b(?:(?:studying|study)(?!\s+in\s+(?:class|grade))|covering|covered|start(?:ing)? with|learn(?:ing)?)\s+([^.;]+)",
        r"\b(?:topic|chapter|concept)\s+(?:is|was|about)\s+([^.;]+)",
        r"\b(?:problem|stuck|confused|weak|struggling|difficulty|difficulties)\s+(?:in|with|on)\s+([^.;]+)",
        r"^\s*(?:in|on)\s+([^.;]+)",
    )
    for message in user_messages:
        lower_message = message.lower()
        for pattern in explicit_patterns:
            for match in re.finditer(pattern, lower_message):
                candidates.extend(_split_learning_list(match.group(1)))

    for message in reversed(user_messages):
        candidate = _short_topic_answer(message)
        if candidate:
            candidates.append(candidate)
            break

    cleaned = [_clean_topic(candidate) for candidate in candidates]
    cleaned = [candidate for candidate in cleaned if candidate]
    return ", ".join(_dedupe_strings(cleaned)[:5]) if cleaned else None


def _extract_language_profile(lower: str) -> dict[str, Any] | None:
    if "hinglish" in lower:
        return {
            "primary": "Hinglish",
            "comfort": "high",
            "explanation_preference": "Hinglish explanations",
        }
    return None


def _clean_topic(raw_topic: str) -> str:
    topic = raw_topic.strip(" .,:;-")
    topic = re.sub(r"\b(?:because|due to|so|but|and)\b.*$", "", topic).strip(" .,:;-")
    topic = re.sub(r"\b(?:i|we)\s+(?:have|am|are|was|were|got)\b", "", topic).strip(" .,:;-")
    topic = re.sub(r"^(?:problem|stuck|confused|weak|struggling)\s+(?:in|with|on)\s+", "", topic, flags=re.I)
    topic = re.sub(r"^(?:in|on|with|for)\s+", "", topic, flags=re.I).strip(" .,:;-")
    topic = re.sub(r"^(?:focus|focusing)\s+on\s+", "", topic, flags=re.I).strip(" .,:;-")
    topic = re.sub(r"\s+", " ", topic)
    if not topic or len(topic) > 80 or _is_low_value_topic(topic) or _looks_like_conversation_fragment(topic):
        return ""
    return _title_topic(topic)


def _split_learning_list(raw: str) -> list[str]:
    cleaned = re.sub(r"\b(?:side by side|alongside|currently|right now)\b", "", raw, flags=re.I)
    parts = re.split(r",|/|\band\b|\bor\b", cleaned)
    return [part.strip(" .,:;-") for part in parts if part.strip(" .,:;-")]


def _short_topic_answer(message: str) -> str | None:
    cleaned = message.strip(" .,:;-")
    words = cleaned.split()
    if not 1 <= len(words) <= 5:
        return None
    if _is_low_value_topic(cleaned) or _looks_like_conversation_fragment(cleaned):
        return None
    return cleaned


def _clean_exam_name(raw_exam: str) -> str:
    exam = raw_exam.strip(" .,:;-")
    exam = _strip_time_context(exam)
    exam = re.sub(
        r"\b(?:exam|exams|which|is|are|on|in|at|side by side|currently|right now|next|my|our|the)\b",
        "",
        exam,
        flags=re.I,
    )
    exam = re.sub(r"\s+", " ", exam).strip(" .,:;-")
    if not exam or len(exam) > 60:
        return ""
    if _looks_like_conversation_fragment(exam) or _is_low_value_topic(exam) or _is_time_only_fragment(exam):
        return ""
    return _title_topic(exam)


def _strip_time_context(value: str) -> str:
    month_names = (
        "jan",
        "january",
        "feb",
        "february",
        "mar",
        "march",
        "apr",
        "april",
        "may",
        "jun",
        "june",
        "jul",
        "july",
        "aug",
        "august",
        "sep",
        "sept",
        "september",
        "oct",
        "october",
        "nov",
        "november",
        "dec",
        "december",
    )
    months = "|".join(month_names)
    cleaned = re.sub(rf"\b\d{{1,2}}(?:st|nd|rd|th)?\s+(?:{months})\b", "", value, flags=re.I)
    cleaned = re.sub(rf"\b(?:{months})\s+\d{{1,2}}(?:st|nd|rd|th)?\b", "", cleaned, flags=re.I)
    cleaned = re.sub(r"\b(?:in|within|after|next)\s+\d+\s+(?:days?|weeks?|months?)\b", "", cleaned, flags=re.I)
    cleaned = re.sub(r"\b\d+\s+(?:days?|weeks?|months?)\b", "", cleaned, flags=re.I)
    cleaned = re.sub(r"\b(?:today|tomorrow|next\s+month|next\s+week|this\s+month|this\s+week)\b", "", cleaned, flags=re.I)
    return cleaned


def _is_time_only_fragment(value: str) -> bool:
    normalized = value.lower().strip(" .,:;-")
    if not normalized:
        return True
    time_words = {
        "day",
        "days",
        "week",
        "weeks",
        "month",
        "months",
        "today",
        "tomorrow",
        "next",
        "this",
    }
    tokens = set(normalized.split())
    return bool(tokens) and tokens <= time_words


def _looks_like_schedule_fragment(value: str) -> bool:
    normalized = value.lower()
    return bool(
        re.search(r"\b(?:also\s+)?(?:in|on|after|within|next|this)\b", normalized)
        and re.search(r"\b(?:\d+|today|tomorrow|day|days|week|weeks|month|months)\b", normalized)
    )


def _looks_like_conversation_fragment(value: str) -> bool:
    normalized = value.lower()
    blocked_fragments = (
        "what is",
        "what can",
        "what should",
        "then what",
        "tell me",
        "can do",
        "problem is about",
        "is about",
        "i have",
        "i am",
        "i want",
        "not remember",
        "don't remember",
        "dont remember",
        "do not remember",
        "not sure",
        "no idea",
        "i don't know",
        "i dont know",
        "also in next",
    )
    return any(fragment in normalized for fragment in blocked_fragments)


def _is_low_value_topic(topic: str) -> bool:
    normalized = topic.lower().strip()
    low_value = {
        "hey",
        "hi",
        "hello",
        "exit",
        "bye",
        "thanks",
        "thank you",
        "yes",
        "no",
        "ok",
        "okay",
        "sure",
        "everything",
        "anything",
        "something",
        "nothing",
        "also",
        "unknown",
        "skip",
        "not remember",
        "dont remember",
        "don't remember",
        "do not remember",
        "not sure",
        "no idea",
        "i don't know",
        "i dont know",
    }
    if normalized in low_value or normalized.isdigit():
        return True
    if re.fullmatch(r"(?:not|dont|don't|do not)\s+remember(?:\s+.*)?", normalized):
        return True
    if re.fullmatch(r"(?:class|grade)\s+\d{1,2}", normalized):
        return True
    if re.fullmatch(r".*\b(?:exam|exams)\b.*", normalized):
        return True
    return False


def _dedupe_strings(values: list[str]) -> list[str]:
    deduped: dict[str, str] = {}
    for value in values:
        normalized = re.sub(r"\s+", " ", value.lower()).strip()
        if normalized:
            deduped.setdefault(normalized, value)
    return list(deduped.values())


def _title_topic(topic: str) -> str:
    small_words = {"and", "or", "of", "in", "to", "the", "for"}
    parts = []
    for word in topic.split():
        parts.append(word if word in small_words else word[:1].upper() + word[1:])
    return " ".join(parts)


def _dedupe_topic_records(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    deduped: dict[str, dict[str, Any]] = {}
    for record in records:
        key = str(record.get("topic") or "").lower()
        if key:
            deduped[key] = record
    return list(deduped.values())


def _merge_list_by_key(current: list[Any], incoming: list[Any], key: str) -> list[dict[str, Any]]:
    merged: dict[str, dict[str, Any]] = {}
    passthrough: list[dict[str, Any]] = []
    for item in current:
        if not isinstance(item, dict):
            continue
        item_key = item.get(key)
        if item_key is None:
            passthrough.append(dict(item))
        else:
            merged[str(item_key).lower()] = dict(item)
    for item in incoming:
        if not isinstance(item, dict):
            continue
        item_key = item.get(key)
        if item_key is None:
            passthrough.append(dict(item))
            continue
        normalized = str(item_key).lower()
        merged[normalized] = {**merged.get(normalized, {}), **item}
    return [*merged.values(), *passthrough]


def _parse_iso_date(value: Any) -> date | None:
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


def _truncate_tokens(text: str, max_tokens: int) -> str:
    if not text:
        return text
    if tiktoken is None:
        words = text.split()
        approx_tokens = int(len(words) * 1.3)
        if approx_tokens <= max_tokens:
            return text
        return " ".join(words[-int(max_tokens / 1.3):])

    encoding = tiktoken.get_encoding("cl100k_base")
    tokens = encoding.encode(text)
    if len(tokens) <= max_tokens:
        return text
    return encoding.decode(tokens[-max_tokens:])


__all__ = ["EdTechExtractionError", "EdTechExtractor"]
