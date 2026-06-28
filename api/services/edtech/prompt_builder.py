from __future__ import annotations

import json
from typing import Any

from api.db.models import EdTechMemory
from api.services.edtech.edtech_schema import NEVER_STORE
from api.services.edtech.edtech_schema import active_fields_for


class EdTechPromptBuilder:
    SCHEMA_DEFINITION = """
Extract the following fields ONLY if clearly evidenced in the conversation.
Do not infer. Do not assume. If unsure: omit the field.

ACADEMIC PROFILE:
- grade_level: string (Class 9, Grade 11, First Year B.Tech, etc.)
- board_or_curriculum: string (CBSE, ICSE, State Board, JEE, NEET, etc.)
- subjects: list of {subject, confidence 1-5, priority high/medium/low, note}
- syllabus_stage: dict of {subject: float 0.0-1.0}

KNOWLEDGE STATE:
- strong_topics: list of {topic, confidence 0-1, evidence, chapter, subject}
- weak_topics: list of {topic, severity mild/moderate/severe, specific_gap, attempts, evidence, chapter, subject}
- concept_gaps: list of {concept, misconception, correct, chapter, subject, status}
- misconceptions: list of {belief, correct, subject, status active/corrected}

LEARNING BEHAVIOUR:
- explanation_style: {primary, secondary, avoid, needs_step_by_step, diagram_helps, code_first, anxiety_trigger}
- language_profile: {primary, comfort, technical_terms, explanation_preference, script}; detect Hinglish/code-switching.
- session_profile: {effective_minutes, disengagement_signal, reengagement, best_session_time}
- peak_hours: {study_time, performance_peak, avoid, session_frequency}

EXAM CONTEXT:
- exam_name: specific exam.
- exam_date: ISO date YYYY-MM-DD only if explicitly stated.
- marks_target: {overall_pct, subject_targets, minimum_acceptable}
- mock_scores: [{date, score, max, subject, test_name}]

PROGRESS TRACKING:
- last_topic_studied: specific topic with chapter context.
- streak: {current_days, longest_days, last_break_reason, weekly_sessions, trend}
"""

    OUTPUT_FORMAT = """
Return JSON only:
{
  "nothing_to_extract": false,
  "extracted": {
    "grade_level": "...",
    "weak_topics": [{"topic": "...", "severity": "moderate", "specific_gap": "..."}]
  },
  "conflicts": [
    {
      "field": "grade_level",
      "existing_value": "Class 10",
      "new_value": "Class 11",
      "resolution": "update",
      "reason": "Student explicitly stated the new grade."
    }
  ],
  "notes": "optional"
}

If the conversation has no durable education memory:
{"nothing_to_extract": true, "extracted": {}, "conflicts": [], "notes": "reason"}
"""

    def build_prompt(
        self,
        conversation: str,
        existing_memory_compressed: str | None,
        is_first_interaction: bool,
        learner_type: str = "school_student",
        active_fields: dict[str, dict[str, Any]] | None = None,
    ) -> str:
        fields = active_fields or active_fields_for(learner_type)
        parts = [
            "You are an EdTech memory extraction specialist. Extract structured learner state from conversations. Return JSON only.",
            f"This learner is classified as: {learner_type}.",
            "Extract only these active fields for this learner type:\n" + _format_active_fields(fields),
            (
                "Evidence rules:\n"
                "- Store durable learner facts only when the user clearly provides evidence.\n"
                "- Do not store assistant suggestions as facts about the learner.\n"
                "- Interpret short user answers using the immediately preceding assistant question.\n"
                "- Preserve the user's meaning across Hinglish, typos, and informal phrasing.\n"
                "- If a phrase could be a subject, goal, exam, or career target, choose the field that matches the user's intent."
            ),
            "Never store:\n" + "\n".join(f"- {item}" for item in NEVER_STORE),
            self.SCHEMA_DEFINITION.strip(),
        ]
        if existing_memory_compressed and not is_first_interaction:
            parts.append(
                "Existing student state:\n"
                f"{existing_memory_compressed}\n\n"
                "If the new evidence contradicts this state, include an item in conflicts[]. "
                "Use resolution='update' for newer/corrected values and resolution='clear' only when the user explicitly says the old value is no longer true."
            )
        parts.extend([self.OUTPUT_FORMAT.strip(), "Conversation:\n" + conversation.strip()])
        return "\n\n".join(parts)

    def compress_existing_memory(self, memory: EdTechMemory | None) -> str | None:
        if memory is None:
            return None
        summary: dict[str, Any] = {}
        for field in (
            "learner_type",
            "grade_level",
            "board_or_curriculum",
            "primary_deadline_event",
            "primary_deadline_date",
            "exam_name",
            "exam_date",
            "last_topic_studied",
        ):
            value = getattr(memory, field, None)
            if value:
                summary[field] = str(value)

        weak_topics = _topic_names(memory.weak_topics, limit=3)
        strong_topics = _topic_names(memory.strong_topics, limit=3)
        if weak_topics:
            summary["weak_topics"] = weak_topics
        if strong_topics:
            summary["strong_topics"] = strong_topics
        if memory.explanation_style:
            summary["explanation_style"] = _compact_dict(memory.explanation_style, ("primary", "needs_step_by_step", "anxiety_trigger"))
        if memory.language_profile:
            summary["language_profile"] = _compact_dict(memory.language_profile, ("primary", "comfort", "explanation_preference"))
        for field in (
            "competitive_exam_context",
            "higher_education_context",
            "professional_cert_context",
            "skill_learner_context",
            "medical_context",
        ):
            value = getattr(memory, field, None)
            if value:
                summary[field] = value
        return json.dumps(summary, default=str, ensure_ascii=True)


def _format_active_fields(fields: dict[str, dict[str, Any]]) -> str:
    lines = []
    for name, spec in fields.items():
        details = []
        if spec.get("description"):
            details.append(str(spec["description"]))
        if spec.get("metadata_keys"):
            details.append("metadata keys: " + ", ".join(str(item) for item in spec["metadata_keys"]))
        if spec.get("allowed_values"):
            details.append("allowed values: " + ", ".join(str(item) for item in spec["allowed_values"]))
        if spec.get("examples"):
            details.append("examples: " + str(spec["examples"]))
        description = "; ".join(details) if details else str(spec.get("content_template") or "")
        lines.append(f"- {name}: {description}")
    return "\n".join(lines)


def _topic_names(items: list[dict[str, Any]] | None, *, limit: int) -> list[str]:
    if not items:
        return []
    names = []
    for item in items[:limit]:
        topic = item.get("topic") or item.get("concept") or item.get("subject")
        if topic:
            names.append(str(topic))
    return names


def _compact_dict(data: dict[str, Any], keys: tuple[str, ...]) -> dict[str, Any]:
    return {key: data[key] for key in keys if data.get(key) is not None}


__all__ = ["EdTechPromptBuilder"]
