from __future__ import annotations

import json
from typing import Any

from api.db.models import SupportMemory
from api.services.support.support_schema import SUPPORT_NEVER_STORE
from api.services.support.support_schema import active_fields_for


class SupportPromptBuilder:
    OUTPUT_FORMAT = """
Return JSON only:
{
  "nothing_to_extract": false,
  "support_type": "one allowed support type",
  "support_type_confidence": 0.0,
  "extracted": {
    "customer_identity": {},
    "communication_preference": {},
    "language_profile": {},
    "current_open_issue": null,
    "issue_history": [],
    "resolution_preference": {},
    "sentiment_pattern": null,
    "risk_signals": [],
    "support_context": {}
  },
  "notes": "optional"
}

If there is no durable customer support memory:
{"nothing_to_extract": true, "extracted": {}, "notes": "reason"}
"""

    def build_prompt(
        self,
        *,
        conversation: str,
        support_type: str,
        support_type_mode: str,
        allowed_support_types: list[str],
        existing_memory_compressed: str | None,
        active_fields: dict[str, dict[str, Any]] | None = None,
    ) -> str:
        fields = active_fields or active_fields_for(support_type)
        type_instruction = _support_type_instruction(
            support_type=support_type,
            support_type_mode=support_type_mode,
            allowed_support_types=allowed_support_types,
        )
        parts = [
            "You are a customer support memory extraction specialist. Extract structured customer support state from conversations. Return JSON only.",
            type_instruction,
            "Generic memory extraction already runs separately. Extract only support-specific structured state.",
            "Do not store transient chit-chat. Do not turn temporary anger into a permanent personality trait.",
            "Separate current open issues from resolved issue history.",
            "Store support-type-specific fields inside support_context.",
            "Extract only these active fields:\n" + _format_active_fields(fields),
            "Never store:\n" + "\n".join(f"- {item}" for item in SUPPORT_NEVER_STORE),
        ]
        if existing_memory_compressed:
            parts.append(
                "Existing support memory:\n"
                f"{existing_memory_compressed}\n\n"
                "Merge new evidence with existing state. Do not clear or overwrite useful existing values with empty values."
            )
        parts.extend([self.OUTPUT_FORMAT.strip(), "Conversation:\n" + conversation.strip()])
        return "\n\n".join(parts)

    def compress_existing_memory(self, memory: SupportMemory | None) -> str | None:
        if memory is None:
            return None
        summary = {
            "support_type": memory.support_type,
            "customer_identity": memory.customer_identity or {},
            "communication_preference": memory.communication_preference or {},
            "language_profile": memory.language_profile or {},
            "current_open_issue": memory.current_open_issue,
            "sentiment_pattern": memory.sentiment_pattern,
            "resolution_preference": memory.resolution_preference or {},
            "risk_signals": list(memory.risk_signals or [])[:5],
            "issue_history_count": len(memory.issue_history or []),
            "support_context": memory.support_context or {},
        }
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
        description = "; ".join(details) if details else str(spec.get("content_template") or "")
        lines.append(f"- {name}: {description}")
    return "\n".join(lines)


def _support_type_instruction(
    *,
    support_type: str,
    support_type_mode: str,
    allowed_support_types: list[str],
) -> str:
    allowed = allowed_support_types or [support_type]
    if support_type_mode == "single":
        return (
            f"This tenant uses one fixed support type: {support_type}. "
            f"Return support_type='{support_type}' and do not classify it differently."
        )
    return (
        f"This tenant uses {support_type_mode} support routing. "
        "Classify the conversation into exactly one allowed support type, then extract fields for that type. "
        "Do not use any type outside this allow-list: "
        + ", ".join(allowed)
        + ". If confidence is below 0.75, return general_info if it is allowed; otherwise return the closest allowed type."
    )


__all__ = ["SupportPromptBuilder"]
