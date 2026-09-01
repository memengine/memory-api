from __future__ import annotations

import asyncio
import copy
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
from api.services.edtech.learner_type_detector import LearnerTypeDetector
from api.services.edtech.prompt_builder import EdTechPromptBuilder
from api.services.edtech.edtech_schema import active_fields_for
from api.services.claim_ledger_service import ClaimLedgerService
from api.services.claim_ledger_service import serialize_claim_value
from api.services.llm_service import LLMService

try:  # pragma: no cover - dependency exists in production, tests can run without it.
    import tiktoken
except ModuleNotFoundError:  # pragma: no cover
    tiktoken = None  # type: ignore[assignment]


LOGGER = logging.getLogger(__name__)
MAX_CONVERSATION_TOKENS = 4000
ARRAY_FIELDS = {
    "subjects",
    "strong_topics",
    "weak_topics",
    "concept_gaps",
    "misconceptions",
    "mock_scores",
}
DICT_FIELDS = {
    "syllabus_stage",
    "explanation_style",
    "session_profile",
    "language_profile",
    "peak_hours",
    "marks_target",
    "streak",
    "progress_trend",
    "competitive_exam_context",
    "higher_education_context",
    "professional_cert_context",
    "skill_learner_context",
    "medical_context",
}
SCALAR_FIELDS = {
    "learner_type",
    "learner_type_confidence",
    "grade_level",
    "board_or_curriculum",
    "primary_goal",
    "primary_deadline_event",
    "primary_deadline_date",
    "exam_name",
    "exam_date",
    "last_topic_studied",
}
ALL_EXTRACTABLE_FIELDS = ARRAY_FIELDS | DICT_FIELDS | SCALAR_FIELDS
EDTECH_CLAIM_CATEGORIES = {
    **{field: "expertise" for field in ARRAY_FIELDS},
    **{field: "fact" for field in DICT_FIELDS},
    **{field: "fact" for field in SCALAR_FIELDS},
    "primary_goal": "goal",
    "primary_deadline_event": "goal",
    "primary_deadline_date": "goal",
    "exam_name": "goal",
    "exam_date": "goal",
    "marks_target": "goal",
    "explanation_style": "preference",
    "session_profile": "preference",
    "language_profile": "preference",
    "peak_hours": "preference",
    "strong_topics": "expertise",
    "weak_topics": "expertise",
    "concept_gaps": "expertise",
    "misconceptions": "expertise",
    "subjects": "expertise",
    "syllabus_stage": "expertise",
    "mock_scores": "expertise",
    "progress_trend": "expertise",
}


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
            provider_clients=None,
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
        raise RuntimeError(
            "extract_and_merge_sync cannot be called from a running event loop."
        )

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

        detection = LearnerTypeDetector().detect_result(
            messages,
            existing_learner_type=_existing_learner_type_for_detection(existing),
        )
        conversation = self._build_conversation(messages)
        compressed = self.prompt_builder.compress_existing_memory(existing)
        prompt = self.prompt_builder.build_prompt(
            conversation=conversation,
            existing_memory_compressed=compressed,
            is_first_interaction=existing is None,
            learner_type=detection.learner_type,
            active_fields=active_fields_for(detection.learner_type),
        )
        fallback_extracted = self._fallback_extract_from_user_text(
            messages, learner_type=detection.learner_type
        )
        fallback_extracted.setdefault("learner_type", detection.learner_type)
        fallback_extracted.setdefault("learner_type_confidence", detection.confidence)
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
            raise EdTechExtractionError(
                "EdTech extraction response field 'extracted' must be an object."
            )

        extracted = _merge_extracted_payload(fallback_extracted, extracted)
        extracted["learner_type"] = detection.learner_type
        extracted["learner_type_confidence"] = detection.confidence
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
        previous_field_values = {
            field: copy.deepcopy(getattr(memory, field, None))
            for field in ALL_EXTRACTABLE_FIELDS
        }
        fields_updated: set[str] = set()
        if getattr(memory, "learner_type", None) is None:
            memory.learner_type = detection.learner_type
            memory.learner_type_confidence = detection.confidence
            fields_updated.update({"learner_type", "learner_type_confidence"})
        conflicts_resolved = self._apply_conflicts(
            memory, data.get("conflicts") or [], fields_updated
        )
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
        claims = self._record_field_claims(
            memory,
            fields_updated=fields_updated,
            job_id=job_id,
        )
        self._restore_non_winning_fields(
            memory,
            claims=claims,
            previous_field_values=previous_field_values,
        )
        self._upsert_memory(memory)

        return EdTechExtractionResult(
            fields_updated=sorted(fields_updated),
            conflicts_resolved=conflicts_resolved,
            nothing_to_extract=False,
            tokens_used=tokens_used,
            provider_used=provider_used,
        )

    def _record_field_claims(
        self,
        memory: EdTechMemory,
        *,
        fields_updated: set[str],
        job_id: str,
    ) -> list[Any]:
        try:
            return ClaimLedgerService(self.session).record_domain_fields(
                domain_record=memory,
                domain="edtech",
                fields_updated=fields_updated,
                field_categories=EDTECH_CLAIM_CATEGORIES,
                job_id=job_id,
            )
        except Exception:
            LOGGER.exception(
                "edtech_claim_ledger_write_failed",
                extra={
                    "event": "edtech_claim_ledger_write_failed",
                    "tenant_id": str(memory.tenant_id),
                    "proxy_user_id": str(memory.proxy_user_id),
                    "job_id": job_id,
                },
            )
            return []

    @staticmethod
    def _restore_non_winning_fields(
        memory: EdTechMemory,
        *,
        claims: list[Any],
        previous_field_values: dict[str, Any],
    ) -> None:
        for claim in claims:
            field = str(claim.predicate_key).removeprefix("edtech.")
            current_value = serialize_claim_value(getattr(memory, field, None))
            if claim.active_value != current_value and field in previous_field_values:
                setattr(memory, field, previous_field_values[field])

    def _upsert_memory(self, memory: EdTechMemory) -> None:
        values = {
            "id": memory.id,
            "proxy_user_id": memory.proxy_user_id,
            "tenant_id": memory.tenant_id,
            "learner_type": memory.learner_type,
            "learner_type_confidence": memory.learner_type_confidence or "high",
            "primary_goal": memory.primary_goal,
            "primary_deadline_event": memory.primary_deadline_event or memory.exam_name,
            "primary_deadline_date": memory.primary_deadline_date or memory.exam_date,
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
            "progress_trend": memory.progress_trend or {},
            "competitive_exam_context": memory.competitive_exam_context or {},
            "higher_education_context": memory.higher_education_context or {},
            "professional_cert_context": memory.professional_cert_context or {},
            "skill_learner_context": memory.skill_learner_context or {},
            "medical_context": memory.medical_context or {},
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
            LOGGER.warning(
                "edtech_invalid_json",
                extra={"event": "edtech_invalid_json", "raw_response": raw[:1000]},
            )
            raise EdTechExtractionError(
                "EdTech extractor returned invalid JSON."
            ) from exc
        if not isinstance(data, dict):
            raise EdTechExtractionError(
                "EdTech extraction response must be a JSON object."
            )
        return data

    def _apply_conflicts(
        self, memory: EdTechMemory, conflicts: list[Any], fields_updated: set[str]
    ) -> int:
        resolved = 0
        for conflict in conflicts:
            if not isinstance(conflict, dict):
                continue
            field = str(conflict.get("field") or "")
            if field not in ALL_EXTRACTABLE_FIELDS:
                continue
            resolution = str(conflict.get("resolution") or "").lower()
            if resolution == "update":
                self._apply_field(
                    memory, field, conflict.get("new_value"), fields_updated
                )
                resolved += 1
            elif resolution == "clear":
                setattr(
                    memory,
                    field,
                    []
                    if field in ARRAY_FIELDS
                    else ({} if field in DICT_FIELDS else None),
                )
                fields_updated.add(field)
                resolved += 1
            if resolution in {"update", "clear"}:
                self.session.add(
                    AuditLog(
                        proxy_user_id=memory.proxy_user_id,
                        action=AuditAction.updated,
                        old_value={
                            "field": field,
                            "existing_value": conflict.get("existing_value"),
                        },
                        new_value={
                            "field": field,
                            "new_value": conflict.get("new_value"),
                            "resolution": resolution,
                        },
                        metadata_json={
                            "event": "edtech_conflict_resolved",
                            "reason": conflict.get("reason"),
                        },
                    )
                )
        return resolved

    def _merge_extracted(
        self, memory: EdTechMemory, extracted: dict[str, Any], fields_updated: set[str]
    ) -> None:
        extracted = _normalize_extracted_payload(dict(extracted or {}))
        for field, value in extracted.items():
            if field not in ALL_EXTRACTABLE_FIELDS or value in (None, "", [], {}):
                continue
            self._apply_field(memory, field, value, fields_updated)

    def _apply_field(
        self, memory: EdTechMemory, field: str, value: Any, fields_updated: set[str]
    ) -> None:
        if field in SCALAR_FIELDS:
            if field in {"exam_date", "primary_deadline_date"}:
                value = _parse_iso_date(value)
                if value is None:
                    return
            setattr(
                memory,
                field,
                str(value)
                if field not in {"exam_date", "primary_deadline_date"}
                else value,
            )
            if field == "exam_name" and not getattr(
                memory, "primary_deadline_event", None
            ):
                memory.primary_deadline_event = str(value)
                fields_updated.add("primary_deadline_event")
            elif field == "exam_date" and not getattr(
                memory, "primary_deadline_date", None
            ):
                memory.primary_deadline_date = value
                fields_updated.add("primary_deadline_date")
            fields_updated.add(field)
            return

        if field in ARRAY_FIELDS:
            current = list(getattr(memory, field) or [])
            if isinstance(value, dict):
                value = [value]
            if not isinstance(value, list):
                return
            setattr(
                memory, field, _merge_list_by_key(current, value, _merge_key_for(field))
            )
            fields_updated.add(field)
            return

        if field in DICT_FIELDS:
            current = dict(getattr(memory, field) or {})
            if not isinstance(value, dict):
                return
            current.update(value)
            setattr(memory, field, current)
            fields_updated.add(field)

    def _update_forgetting_stages(
        self, memory: EdTechMemory, extracted: dict[str, Any]
    ) -> None:
        stages = dict(memory.forgetting_stages or {})
        today = date.today().isoformat()
        touched_topics = set()
        for field in ("strong_topics", "weak_topics"):
            value = extracted.get(field)
            if isinstance(value, list):
                for item in value:
                    if isinstance(item, dict) and item.get("topic"):
                        touched_topics.add(str(item["topic"]))
        if memory.last_topic_studied and not _is_low_value_topic(
            str(memory.last_topic_studied)
        ):
            touched_topics.add(str(memory.last_topic_studied))

        known_topics = set(touched_topics)
        for item in list(memory.strong_topics or []) + list(memory.weak_topics or []):
            if isinstance(item, dict) and item.get("topic"):
                known_topics.add(str(item["topic"]))

        for topic in known_topics:
            current = dict(stages.get(topic) or {})
            if topic in touched_topics:
                current.update(
                    {
                        "stage": "fresh",
                        "last_reviewed": today,
                        "days_since": 0,
                        "review_due": today,
                    }
                )
            else:
                last_reviewed = _parse_iso_date(current.get("last_reviewed"))
                days = (date.today() - last_reviewed).days if last_reviewed else 999
                current.update(
                    {
                        "stage": compute_forgetting_stage(days),
                        "days_since": max(0, days),
                    }
                )
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

    def _fallback_extract_from_user_text(
        self,
        messages: list[dict[str, Any]],
        learner_type: str | None = None,
    ) -> dict[str, Any]:
        """High-precision fallback for obvious identity/deadline facts only.

        Domain semantics such as weak topics, learning style, goals, and strategy
        belong to the LLM structured extractor. Keeping this fallback narrow avoids
        turning EdTech into a brittle regex product.
        """
        user_text = ". ".join(
            str(message.get("content") or "").strip()
            for message in messages
            if str(message.get("role") or "user").lower() == "user"
        )
        lower = _normalize_learning_text(user_text.lower())
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
            extracted["primary_deadline_event"] = exam_name

        exam_date = _extract_explicit_exam_date(lower)
        if exam_date:
            extracted["exam_date"] = exam_date.isoformat()
            extracted["primary_deadline_date"] = exam_date.isoformat()
            deadline_event = _extract_deadline_event_for_date(lower)
            current_event = str(extracted.get("primary_deadline_event") or "").lower()
            if deadline_event and (
                not current_event or current_event in {"board", "boards"}
            ):
                if deadline_event:
                    extracted["primary_deadline_event"] = deadline_event
                    extracted.setdefault("exam_name", deadline_event)

        return extracted


def _merge_key_for(field: str) -> str:
    return {
        "subjects": "subject",
        "concept_gaps": "concept",
        "misconceptions": "belief",
        "mock_scores": "date",
    }.get(field, "topic")


def _merge_extracted_payload(
    fallback: dict[str, Any], model_extracted: dict[str, Any]
) -> dict[str, Any]:
    merged = _normalize_extracted_payload(dict(fallback or {}))
    for field, value in _normalize_extracted_payload(
        dict(model_extracted or {})
    ).items():
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
    return _sanitize_extracted_payload(merged)


def _normalize_extracted_payload(payload: dict[str, Any]) -> dict[str, Any]:
    if "weak_areas" in payload and "weak_topics" not in payload:
        payload["weak_topics"] = payload.pop("weak_areas")
    if "strong_areas" in payload and "strong_topics" not in payload:
        payload["strong_topics"] = payload.pop("strong_areas")

    deadline = payload.pop("deadline", None)
    if isinstance(deadline, dict):
        if deadline.get("event") and not payload.get("primary_deadline_event"):
            payload["primary_deadline_event"] = deadline["event"]
        if deadline.get("date") and not payload.get("primary_deadline_date"):
            payload["primary_deadline_date"] = deadline["date"]

    for extension_field, context_field in (
        ("exam_details", "competitive_exam_context"),
        ("cut_off_target", "competitive_exam_context"),
        ("subject_strategy", "competitive_exam_context"),
        ("current_resource", "competitive_exam_context"),
        ("academic_context", "higher_education_context"),
        ("backlog_subjects", "higher_education_context"),
        ("project_work", "higher_education_context"),
        ("placement_target", "higher_education_context"),
        ("certification_context", "professional_cert_context"),
        ("paper_strategy", "professional_cert_context"),
        ("study_group", "professional_cert_context"),
        ("current_skill_level", "skill_learner_context"),
        ("current_project", "skill_learner_context"),
        ("learning_path_stage", "skill_learner_context"),
        ("error_patterns", "skill_learner_context"),
        ("job_target", "skill_learner_context"),
        ("medical_context", "medical_context"),
        ("pg_entrance_target", "medical_context"),
        ("high_yield_subjects", "medical_context"),
        ("clinical_context", "medical_context"),
    ):
        value = payload.pop(extension_field, None)
        if isinstance(value, dict):
            current = dict(payload.get(context_field) or {})
            current[extension_field] = value
            payload[context_field] = current
        elif isinstance(value, list):
            current = dict(payload.get(context_field) or {})
            current[extension_field] = value
            payload[context_field] = current
    return payload


def _sanitize_extracted_payload(payload: dict[str, Any]) -> dict[str, Any]:
    sanitized = dict(payload)
    for field in ("exam_name", "primary_deadline_event"):
        value = sanitized.get(field)
        if not value:
            continue
        cleaned = _clean_exam_name(str(value))
        if cleaned:
            sanitized[field] = cleaned
        else:
            sanitized.pop(field, None)

    if isinstance(sanitized.get("subjects"), list):
        subjects = []
        for item in sanitized["subjects"]:
            if not isinstance(item, dict):
                continue
            subject = _clean_topic(str(item.get("subject") or ""))
            if (
                not subject
                or _looks_like_exam_label(subject)
                or _looks_like_non_subject_label(subject)
            ):
                continue
            subjects.append({**item, "subject": subject})
        sanitized["subjects"] = _dedupe_subject_records(subjects)

    if isinstance(sanitized.get("weak_topics"), list):
        weak_topics = []
        for item in sanitized["weak_topics"]:
            if not isinstance(item, dict):
                continue
            topic = _clean_topic(str(item.get("topic") or ""))
            if not topic or _looks_like_exam_label(topic):
                continue
            weak_topics.append({**item, "topic": topic})
        sanitized["weak_topics"] = _dedupe_topic_records(weak_topics)

    if isinstance(sanitized.get("strong_topics"), list):
        strong_topics = []
        for item in sanitized["strong_topics"]:
            if not isinstance(item, dict):
                continue
            topic = _clean_topic(str(item.get("topic") or ""))
            if not topic or _looks_like_exam_label(topic):
                continue
            strong_topics.append({**item, "topic": topic})
        sanitized["strong_topics"] = _dedupe_topic_records(strong_topics)

    if sanitized.get("last_topic_studied"):
        cleaned_topics = [
            _clean_topic(topic)
            for topic in _split_learning_list(str(sanitized["last_topic_studied"]))
        ]
        cleaned_topics = [
            topic
            for topic in cleaned_topics
            if topic and not _looks_like_exam_label(topic)
        ]
        if cleaned_topics:
            sanitized["last_topic_studied"] = ", ".join(
                _dedupe_strings(cleaned_topics)[:5]
            )
        else:
            sanitized.pop("last_topic_studied", None)
    return sanitized


def _add_extension_context(extracted: dict[str, Any], learner_type: str | None) -> None:
    if learner_type == "competitive_exam":
        context: dict[str, Any] = {}
        if extracted.get("exam_name"):
            context["exam_details"] = {"exam_name": extracted["exam_name"]}
        if context:
            extracted["competitive_exam_context"] = context
    elif learner_type == "higher_education":
        context = _higher_education_context_from_fallback(extracted)
        if context:
            extracted["higher_education_context"] = context
    elif learner_type == "skill_learner":
        context = {}
        if extracted.get("subjects"):
            context["current_skill_level"] = {
                "skills": [
                    item.get("subject")
                    for item in extracted["subjects"]
                    if item.get("subject")
                ]
            }
        if context:
            extracted["skill_learner_context"] = context


def _normalize_learning_text(text: str) -> str:
    """Normalize common learner typo noise before conservative pattern matching."""
    replacements = {
        r"\bborad\b": "board",
        r"\bengilsh\b": "english",
        r"\bstyding\b": "studying",
        r"\bvedio\b": "video",
    }
    normalized = text
    for pattern, replacement in replacements.items():
        normalized = re.sub(pattern, replacement, normalized, flags=re.I)
    return normalized


def _existing_learner_type_for_detection(memory: EdTechMemory | None) -> str | None:
    if memory is None:
        return None
    if not getattr(memory, "learner_type", None):
        return None
    return memory.learner_type if _has_substantive_edtech_profile(memory) else None


def _has_substantive_edtech_profile(memory: EdTechMemory) -> bool:
    scalar_fields = (
        "grade_level",
        "board_or_curriculum",
        "primary_goal",
        "primary_deadline_event",
        "primary_deadline_date",
        "exam_name",
        "exam_date",
        "last_topic_studied",
    )
    if any(getattr(memory, field, None) for field in scalar_fields):
        return True

    collection_fields = (
        "subjects",
        "strong_topics",
        "weak_topics",
        "concept_gaps",
        "misconceptions",
        "mock_scores",
        "syllabus_stage",
        "explanation_style",
        "session_profile",
        "language_profile",
        "peak_hours",
        "marks_target",
        "streak",
        "progress_trend",
        "competitive_exam_context",
        "higher_education_context",
        "professional_cert_context",
        "skill_learner_context",
        "medical_context",
        "forgetting_stages",
        "improvement_velocity",
    )
    return any(bool(getattr(memory, field, None)) for field in collection_fields)


def _higher_education_context_from_fallback(
    extracted: dict[str, Any],
) -> dict[str, Any]:
    context: dict[str, Any] = {}
    grade = str(extracted.get("grade_level") or "")
    year_match = re.match(
        r"(?P<year>(?:\d+(?:st|nd|rd|th)|Final))\s+Year(?:\s+(?P<branch>.+))?", grade
    )
    if year_match:
        context["academic_context"] = {
            "year": year_match.group("year"),
            "branch": year_match.group("branch"),
        }
    semester_match = re.match(r"Semester\s+(?P<semester>\d{1,2})", grade)
    if semester_match:
        context["academic_context"] = {
            **dict(context.get("academic_context") or {}),
            "semester": int(semester_match.group("semester")),
        }
    if extracted.get("exam_name"):
        context["deadline_context"] = {"event": extracted["exam_name"]}
    if extracted.get("primary_goal"):
        context["academic_goal"] = {"goal": extracted["primary_goal"]}
    return context


def _extract_grade_level(lower: str) -> str | None:
    match = re.search(r"\b(?:class|grade)\s*[-:]?\s*(\d{1,2})\b", lower)
    if not match:
        match = re.search(r"\b(\d{1,2})(?:st|nd|rd|th)?\s+(?:class|grade)\b", lower)
    if match:
        grade = int(match.group(1))
        if 1 <= grade <= 12:
            return f"Class {grade}"

    semester_match = re.search(
        r"\b(?P<semester>\d{1,2})\s*(?:st|nd|rd|th)?\s*(?:sem|semester)\b", lower
    )
    if semester_match:
        semester = int(semester_match.group("semester"))
        if 1 <= semester <= 12:
            return f"Semester {semester}"

    year_match = re.search(
        r"\b(?:i am|i'm|im|doing|pursuing)?\s*(?:a\s+)?"
        r"(?:(?:b\.?\s?tech|betech|engineering)\s+)?"
        r"(?P<year>\d{1,2}|first|second|third|fourth|final)"
        r"(?:st|nd|rd|th)?\s+year"
        r"(?:\s+(?P<program>[a-z][a-z0-9&.+ -]{1,40}?))?"
        r"\s+student\b",
        lower,
    )
    if year_match:
        year = _format_academic_year(year_match.group("year"))
        program = _clean_program_name(year_match.group("program") or "")
        return f"{year} Year {program}".strip()
    return None


def _extract_primary_goal(lower: str) -> str | None:
    if re.search(
        r"\b(?:increase|improve|boost|raise)\s+(?:my\s+|the\s+)?(?:cpi|cgpa|sgpa|gpa)\b",
        lower,
    ):
        return "Improve academic performance score"
    if re.search(
        r"\b(?:spoken\s+english|english\s+speaking|improve\s+(?:my\s+)?english|practice\s+(?:my\s+)?english)\b",
        lower,
    ):
        return "Improve spoken English"
    if re.search(
        r"\b(?:focus(?:ing)? on|prepar(?:ing)? for|target(?:ing)?|want(?: to)?\s+(?:pursue|do|study|get into))\s+(?:an?\s+)?mba\b",
        lower,
    ):
        return "Prepare for MBA"
    if re.search(r"\b(?:crack|clear|pass|get selected in|qualify)\b", lower):
        exam_name = _extract_exam_name(lower)
        return f"Clear {exam_name}" if exam_name else "Clear target exam"
    return None


def _extract_board_or_curriculum(lower: str) -> str | None:
    board_match = re.search(
        r"\b(cbse|icse|state board|ncert)\b(?:\s+board)?(?:\s+exam)?\b", lower
    )
    if board_match:
        return _title_topic(board_match.group(1))

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
        r"\b(?:(?:prepar|preap)\w* for|target(?:ing)?|crack(?:ing)?|appearing for)\s+([^.;!?]{2,80})",
        r"\b(?:(?:prepar|preap)\w* for|target(?:ing)?|crack(?:ing)?|appearing for|focusing on)\s+([^.;!?]{2,80}?\bexam(?:s)?\b)",
        r"\b(?:my|our|the)?\s*([a-z0-9&+ -]{2,80}?\b(?:board|jee|iit|neet|cuet|gate|upsc|ssc|cat|gre|gmat|exam)(?:\s+exam(?:s)?)?)\s+(?:is|are|on|in|starts?|scheduled|which\s+is)\b",
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
    cleaned.extend(_extract_exam_acronyms(lower))
    if re.search(
        r"\b(?:semester|sem)\s+exam\b|\bexam\s+(?:is\s+)?(?:on|in)\b", lower
    ) and re.search(
        r"\b(?:sem|semester|college|university|cpi|cgpa|sgpa|btech|betech|b\.tech|engineering)\b",
        lower,
    ):
        cleaned.append("Semester")
    return " + ".join(_dedupe_strings(cleaned)[:5]) if cleaned else None


def _extract_deadline_event_for_date(lower: str) -> str | None:
    if re.search(r"\bboards?\s+exam\b", lower):
        return "Board Exam"
    if re.search(r"\b(?:semester|sem)\s+exam\b", lower):
        return "Semester"
    return _extract_exam_name(lower)


def _extract_exam_acronyms(lower: str) -> list[str]:
    exams = {
        "jee main": "JEE Main",
        "jee mains": "JEE Main",
        "jee": "JEE",
        "iit": "IIT",
        "neet": "NEET",
        "cuet": "CUET",
        "gate": "GATE",
        "upsc": "UPSC",
        "ssc": "SSC",
        "cat": "CAT",
        "gre": "GRE",
        "gmat": "GMAT",
    }
    found: list[str] = []
    matched_raw: set[str] = set()
    for raw, display in sorted(
        exams.items(), key=lambda item: len(item[0]), reverse=True
    ):
        if raw == "jee" and ({"jee main", "jee mains"} & matched_raw):
            continue
        if re.search(rf"\b{re.escape(raw)}\b", lower):
            found.append(display)
            matched_raw.add(raw)
    return _dedupe_strings(found)


def _extract_explicit_exam_date(lower: str) -> date | None:
    if not re.search(
        r"\b(?:exam|test|deadline|paper|attempt|boards?|jee|iit|neet|cuet|gate)\b",
        lower,
    ):
        return None

    month_pattern = _month_name_pattern()
    patterns = (
        rf"\b(?:on|by|before)\s+(?P<day>\d{{1,2}})(?:st|nd|rd|th)?\s+(?P<month>{month_pattern})(?:\s*,?\s*(?P<year>\d{{4}}))?\b",
        rf"\b(?P<day>\d{{1,2}})(?:st|nd|rd|th)?\s+(?P<month>{month_pattern})(?:\s*,?\s*(?P<year>\d{{4}}))?\b",
        rf"\b(?P<month>{month_pattern})\s+(?P<day>\d{{1,2}})(?:st|nd|rd|th)?(?:\s*,?\s*(?P<year>\d{{4}}))?\b",
    )
    today = date.today()
    for pattern in patterns:
        match = re.search(pattern, lower)
        if not match:
            continue
        month = _month_number(match.group("month"))
        day = int(match.group("day"))
        if month is None:
            continue
        try:
            year = match.groupdict().get("year")
            parsed = date(int(year) if year else today.year, month, day)
        except ValueError:
            continue
        if year:
            return parsed
        return parsed if parsed >= today else date(today.year + 1, month, day)
    return None


def _extract_subjects(lower: str) -> list[dict[str, Any]]:
    candidates: list[str] = []
    list_patterns = (
        r"\b(?:subjects?|topics?)\s+(?:are|is)\s+([^.;]+)",
        r"\b(?:studying|study|covering)\s+([^.;]+)",
    )
    for pattern in list_patterns:
        for match in re.finditer(pattern, lower):
            candidates.extend(_split_learning_list(match.group(1)))

    subjects = []
    for candidate in _dedupe_strings(candidates):
        subject = _clean_topic(candidate)
        if (
            not subject
            or _looks_like_exam_label(subject)
            or _looks_like_non_subject_label(subject)
        ):
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


def _extract_strong_topics(lower: str) -> list[dict[str, Any]]:
    strong_topics: list[dict[str, Any]] = []
    patterns = (
        r"\b([^.,;]+?)\s+(?:is|are|feel|feels)?\s*(?:my\s+)?(?:strong|strength|good|comfortable|confident)\b",
        r"\b(?:strong|good|comfortable|confident)\s+(?:in|with|at)\s+([^.,;]+)",
    )
    for pattern in patterns:
        for match in re.finditer(pattern, lower):
            for candidate in _split_learning_list(match.group(1)):
                topic = _clean_topic(candidate)
                if not topic or _looks_like_exam_label(topic):
                    continue
                strong_topics.append(
                    {
                        "topic": topic,
                        "confidence": 0.75,
                        "evidence": "Student described this as a strength.",
                    }
                )
    return _dedupe_topic_records(strong_topics)


def _extract_weak_topics(lower: str) -> list[dict[str, Any]]:
    weak_topics: list[dict[str, Any]] = []
    patterns = (
        r"\bproblem\s+(?:in|with|on)\s+([^.,;]+)",
        r"\b(?:stuck|confused|weak|struggling|difficulty|difficulties)\s+(?:everytime\s+)?(?:in|with|on)\s+([^.,;]+)",
        r"\b(?:poor|bad|not good)\s+(?:in|at|with)\s+([^.,;]+)",
        r"\b(?:except|other than)\s+([^.,;]+)",
        r"\b(?:want|wants|need|needs|trying)\s+(?:to\s+)?(?:improve|practice|work on)\s+(?:my\s+)?([^.,;]+)",
        r"\b(?:improve|practice|work on)\s+(?:my\s+)?([^.,;]+)",
        r"\b(?:hurdle|challenge|problem)\s+(?:is|was)\s+([^.,;]+)",
        r"\b([^.,;]+?)\s+(?:is|feels|looks)\s+(?:really\s+|very\s+|too\s+|so\s+)?(?:hard|difficult|confusing|impossible)\b",
        r"\b(?:hard|difficult)\s+(?:topic|subject)\s+(?:is\s+)?([^.,;]+)",
        r"\b([^.,;]+?)\s+(?:make|makes|made)\s+(?:me|us)?\s*(?:feel\s+|in\s+)?(?:frustrated|frustration|underconfident|lose confidence|loose confident)\b",
    )
    for pattern in patterns:
        for match in re.finditer(pattern, lower):
            raw_topic = _clean_topic(_focus_weak_candidate(match.group(1)))
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
    if re.search(
        r"\b(?:not able to|unable to|can't|cannot)\s+(?:interpret|interrupt|solve|apply)\s+(?:questions?|problems?)\b",
        lower,
    ):
        weak_topics.append(
            {
                "topic": "Question Solving",
                "severity": "moderate",
                "attempts": 1,
                "specific_gap": "Student understands concepts but struggles to interpret or apply them in questions.",
                "evidence": "Detected from the student's own statement.",
            }
        )
    if re.search(
        r"\b(?:spoken\s+english|english\s+speaking|speaking\s+english)\b", lower
    ):
        weak_topics.append(
            {
                "topic": "Spoken English",
                "severity": "moderate",
                "attempts": 1,
                "specific_gap": "Student wants support improving spoken English.",
                "evidence": "Detected from the student's own statement.",
            }
        )
    return _dedupe_topic_records(weak_topics)


def _focus_weak_candidate(candidate: str) -> str:
    """In contrastive phrasing, the weak area usually appears after the contrast marker."""
    parts = re.split(r"\b(?:but|except|however)\b", candidate, flags=re.I)
    return parts[-1] if parts else candidate


def _extract_last_topic_studied(user_messages: list[str]) -> str | None:
    candidates: list[str] = []
    explicit_patterns = (
        r"\b(?:(?:studying|study)(?!\s+in\s+(?:class|grade))|covering|covered|start(?:ing)? with|learn(?:ing)?)\s+([^.;]+)",
        r"\b(?:need|needs|want|wants|should)?\s*(?:to\s+)?focus\s+(?:on|for)\s+([^.;]+)",
        r"\b(?:topic|chapter|concept)\s+(?:is|was|about)\s+([^.;]+)",
        r"\b(?:problem|stuck|confused|weak|struggling|difficulty|difficulties)\s+(?:in|with|on)\s+([^.;]+)",
        r"\b([^.,;]+?)\s+(?:is|feels|looks)\s+(?:really\s+|very\s+|too\s+|so\s+)?(?:hard|difficult|confusing|impossible)\b",
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
    if re.search(
        r"\b(?:poor|bad|weak|not good)\s+(?:in|at|with)\s+english\b", lower
    ) or re.search(
        r"\b(?:spoken\s+english|english\s+speaking|improve\s+(?:my\s+)?english|practice\s+(?:my\s+)?english)\b",
        lower,
    ):
        return {
            "primary": "English",
            "comfort": "low",
            "explanation_preference": "Use simpler language and bilingual support when helpful",
        }
    return None


def _clean_topic(raw_topic: str) -> str:
    topic = raw_topic.strip(" .,:;-+")
    topic = re.sub(r"\b(?:because|due to|so|but|and)\b.*$", "", topic).strip(" .,:;-")
    topic = re.sub(r"\b(?:i|we)\s+(?:have|am|are|was|were|got)\b", "", topic).strip(
        " .,:;-"
    )
    topic = re.sub(
        r"^(?:need|needs|want|wants|should)?\s*(?:to\s+)?(?:focus|study|prepare)\s+(?:on|for)?\s+",
        "",
        topic,
        flags=re.I,
    ).strip(" .,:;-")
    topic = re.sub(
        r"^(?:problem|stuck|confused|weak|struggling)\s+(?:in|with|on)\s+",
        "",
        topic,
        flags=re.I,
    )
    topic = re.sub(r"^(?:in|on|with|for)\s+", "", topic, flags=re.I).strip(" .,:;-")
    topic = re.sub(r"^(?:focus|focusing)\s+on\s+", "", topic, flags=re.I).strip(
        " .,:;-"
    )
    topic = re.sub(
        r"\b(?:any\s+)?(?:strategy|straitgy|stratgy|tips?|plan|help)\b.*$",
        "",
        topic,
        flags=re.I,
    ).strip(" .,:;-")
    topic = re.sub(r"\b(?:also|too)\b$", "", topic, flags=re.I).strip(" .,:;-")
    topic = re.sub(r"\s+", " ", topic)
    if (
        not topic
        or len(topic) > 80
        or _is_low_value_topic(topic)
        or _looks_like_conversation_fragment(topic)
    ):
        return ""
    return _title_topic(topic)


def _format_academic_year(raw_year: str) -> str:
    normalized = raw_year.lower()
    word_to_number = {
        "first": 1,
        "second": 2,
        "third": 3,
        "fourth": 4,
        "final": None,
    }
    if normalized == "final":
        return "Final"
    year_number = word_to_number.get(normalized)
    if year_number is None:
        year_number = int(normalized)
    suffix = "th"
    if year_number % 10 == 1 and year_number % 100 != 11:
        suffix = "st"
    elif year_number % 10 == 2 and year_number % 100 != 12:
        suffix = "nd"
    elif year_number % 10 == 3 and year_number % 100 != 13:
        suffix = "rd"
    return f"{year_number}{suffix}"


def _clean_program_name(raw_program: str) -> str:
    program = raw_program.strip(" .,:;-")
    if not program:
        return ""
    program = re.sub(r"\b(?:and|who|with|from|at)\b.*$", "", program, flags=re.I).strip(
        " .,:;-"
    )
    if not program or _looks_like_conversation_fragment(program):
        return ""
    if len(program) <= 6 and re.fullmatch(r"[a-z0-9&.+ -]+", program):
        return program.upper()
    return _title_topic(program)


def _split_learning_list(raw: str) -> list[str]:
    cleaned = re.sub(
        r"\b(?:side by side|alongside|currently|right now|paralle?ly)\b",
        "",
        raw,
        flags=re.I,
    )
    parts = re.split(r",|/|\bas well as\b|\band also\b|\band\b|\bor\b", cleaned)
    expanded: list[str] = []
    for part in parts:
        cleaned_part = part.strip(" .,:;-")
        if not cleaned_part:
            continue
        expanded.extend(_split_packed_subjects(cleaned_part))
    return expanded


def _split_packed_subjects(raw: str) -> list[str]:
    """Split simple subject runs like 'physics chemistry' without over-splitting phrases."""
    normalized = raw.lower().strip()
    subject_aliases = {
        "math",
        "maths",
        "mathematics",
        "mathmatics",
        "physics",
        "chemistry",
        "chemestry",
        "biology",
        "english",
        "history",
        "geography",
        "economics",
        "accounts",
        "accounting",
        "computer science",
        "cs",
    }
    found = [
        alias
        for alias in sorted(subject_aliases, key=len, reverse=True)
        if re.search(rf"\b{re.escape(alias)}\b", normalized)
    ]
    if len(found) >= 2 and len(normalized.split()) <= len(found) + 2:
        return [_title_topic(alias) for alias in found]
    return [raw]


def _short_topic_answer(message: str) -> str | None:
    cleaned = message.strip(" .,:;-")
    words = cleaned.split()
    if not 1 <= len(words) <= 5:
        return None
    if _is_low_value_topic(cleaned) or _looks_like_conversation_fragment(cleaned):
        return None
    return cleaned


def _clean_exam_name(raw_exam: str) -> str:
    exam = raw_exam.strip(" .,:;-+")
    exam = re.sub(r"\b(?:so|because|but|then)\b.*$", "", exam, flags=re.I).strip(
        " .,:;-+"
    )
    exam = re.sub(
        r"^(?:paralle?ly\s+)?(?:(?:prepar|preap)\w* for|target(?:ing)?|crack(?:ing)?|appearing for|focus(?:ing)? on)\s+",
        "",
        exam,
        flags=re.I,
    ).strip(" .,:;-+")
    exam = _strip_time_context(exam)
    exam = re.sub(
        r"\b(?:exam|exams|which|is|are|on|in|at|side by side|currently|right now|next|my|our|the)\b",
        "",
        exam,
        flags=re.I,
    )
    exam = re.sub(r"\s+", " ", exam).strip(" .,:;-+")
    if not exam or len(exam) > 60:
        return ""
    if (
        _looks_like_conversation_fragment(exam)
        or _is_low_value_topic(exam)
        or _is_time_only_fragment(exam)
        or _looks_like_non_subject_label(exam)
    ):
        return ""
    return _title_topic(exam)


def _month_name_pattern() -> str:
    return (
        "jan|january|feb|february|mar|march|apr|april|may|jun|june|jul|july|"
        "aug|august|sep|sept|september|oct|october|nov|november|novemeber|"
        "dec|december"
    )


def _month_number(value: str) -> int | None:
    normalized = value.lower().strip()
    month_map = {
        "jan": 1,
        "january": 1,
        "feb": 2,
        "february": 2,
        "mar": 3,
        "march": 3,
        "apr": 4,
        "april": 4,
        "may": 5,
        "jun": 6,
        "june": 6,
        "jul": 7,
        "july": 7,
        "aug": 8,
        "august": 8,
        "sep": 9,
        "sept": 9,
        "september": 9,
        "oct": 10,
        "october": 10,
        "nov": 11,
        "november": 11,
        "novemeber": 11,
        "dec": 12,
        "december": 12,
    }
    return month_map.get(normalized)


def _strip_time_context(value: str) -> str:
    months = _month_name_pattern()
    cleaned = re.sub(
        rf"\b\d{{1,2}}(?:st|nd|rd|th)?\s+(?:{months})\b", "", value, flags=re.I
    )
    cleaned = re.sub(
        rf"\b(?:{months})\s+\d{{1,2}}(?:st|nd|rd|th)?\b", "", cleaned, flags=re.I
    )
    cleaned = re.sub(
        r"\b(?:in|within|after|next)\s+\d+\s+(?:days?|weeks?|months?)\b",
        "",
        cleaned,
        flags=re.I,
    )
    cleaned = re.sub(r"\b\d+\s+(?:days?|weeks?|months?)\b", "", cleaned, flags=re.I)
    cleaned = re.sub(
        r"\b(?:today|tomorrow|next\s+month|next\s+week|this\s+month|this\s+week)\b",
        "",
        cleaned,
        flags=re.I,
    )
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
        and re.search(
            r"\b(?:\d+|today|tomorrow|day|days|week|weeks|month|months)\b", normalized
        )
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


def _looks_like_exam_label(value: str) -> bool:
    normalized = value.lower().strip(" .,:;-")
    return bool(
        re.fullmatch(
            r"(?:board|boards|jee|jee main|jee mains|neet|cuet|gate|upsc|ssc|cat|gre|gmat|sat|act|exam|exams)",
            normalized,
        )
    )


def _looks_like_non_subject_label(value: str) -> bool:
    normalized = value.lower().strip(" .,:;-")
    return bool(
        re.fullmatch(
            r"(?:.*\bexam(?:s)?\b|mba|bba|mca|bca|btech|b\.tech|mtech|m\.tech|job|career|placement|internship)",
            normalized,
        )
    )


def _is_low_value_topic(topic: str) -> bool:
    normalized = topic.lower().strip()
    low_value = {
        "hey",
        "hi",
        "hello",
        "exit",
        "i",
        "me",
        "we",
        "us",
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
    if re.fullmatch(
        r"(?:mostly|usually|often)\s+at\s+(?:night|morning|evening)", normalized
    ):
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
    acronyms = {
        "act",
        "cat",
        "cbse",
        "cuet",
        "gate",
        "gmat",
        "gre",
        "icse",
        "iit",
        "jee",
        "neet",
        "ncert",
        "sat",
        "ssc",
        "upsc",
    }
    small_words = {"and", "or", "of", "in", "to", "the", "for"}
    parts = []
    for word in topic.split():
        if word.lower() in acronyms:
            parts.append(word.upper())
        else:
            parts.append(word if word in small_words else word[:1].upper() + word[1:])
    return " ".join(parts)


def _dedupe_subject_records(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    deduped: dict[str, dict[str, Any]] = {}
    for record in records:
        key = str(record.get("subject") or "").lower()
        if key:
            deduped[key] = record
    return list(deduped.values())


def _dedupe_topic_records(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    deduped: dict[str, dict[str, Any]] = {}
    for record in records:
        key = str(record.get("topic") or "").lower()
        if key:
            deduped[key] = record
    return list(deduped.values())


def _merge_list_by_key(
    current: list[Any], incoming: list[Any], key: str
) -> list[dict[str, Any]]:
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
        return " ".join(words[-int(max_tokens / 1.3) :])

    encoding = tiktoken.get_encoding("cl100k_base")
    tokens = encoding.encode(text)
    if len(tokens) <= max_tokens:
        return text
    return encoding.decode(tokens[-max_tokens:])


__all__ = ["EdTechExtractionError", "EdTechExtractor"]
