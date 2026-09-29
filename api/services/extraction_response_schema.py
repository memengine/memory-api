from __future__ import annotations

from typing import Any

CATEGORIES = [
    "preference",
    "fact",
    "goal",
    "procedure",
    "relationship",
    "expertise",
]


def build_extraction_response_schema(
    *,
    proposal_confirmation_enabled: bool,
) -> dict[str, Any]:
    """Build the strict primary-extraction response schema.

    The schema controls shape only. Evidence, authority, and write eligibility
    remain server-validated after the provider returns.
    """

    properties: dict[str, Any] = {}
    required: list[str] = []
    if proposal_confirmation_enabled:
        properties["proposal_confirmation"] = _nullable_object(
            {
                "decision": {
                    "type": "string",
                    "enum": ["confirmed", "rejected", "ambiguous", "unrelated"],
                },
                "target_ordinal": {"type": ["integer", "null"]},
                "selection_evidence": {"type": ["string", "null"]},
            }
        )
        required.append("proposal_confirmation")

    properties["memory_clarification"] = _nullable_object(
        {
            "requested": {"type": "boolean"},
            "memory_ids": {"type": "array", "items": {"type": "string"}},
            "evidence_turn": {"type": "integer"},
            "selection_evidence": {"type": "string"},
        }
    )
    properties["memories"] = {
        "type": "array",
        "items": _memory_item_schema(
            proposal_confirmation_enabled=proposal_confirmation_enabled
        ),
    }
    required.extend(["memory_clarification", "memories"])

    properties["nothing_to_extract"] = {"type": "boolean"}
    properties["extraction_notes"] = {"type": ["string", "null"]}
    required.extend(["nothing_to_extract", "extraction_notes"])
    return {
        "type": "object",
        "properties": properties,
        "required": required,
        "additionalProperties": False,
    }


def _memory_item_schema(*, proposal_confirmation_enabled: bool) -> dict[str, Any]:
    evidence_relations = (
        ["direct_user_statement"]
        if proposal_confirmation_enabled
        else ["direct_user_statement", "user_confirmed_assistant_proposal"]
    )
    properties = {
        "content": {"type": "string"},
        "category": {"type": "string", "enum": CATEGORIES},
        "importance_score": {"type": "number", "minimum": 1.0, "maximum": 10.0},
        "confidence": {"type": "number", "minimum": 0.0, "maximum": 1.0},
        "commitment": {
            "type": "string",
            "enum": ["asserted", "uncertain_change"],
        },
        "evidence_turns": {"type": "array", "items": {"type": "integer"}},
        "evidence_relation": {"type": "string", "enum": evidence_relations},
        "proposal_turn": {"type": ["integer", "null"]},
        "reasoning": {"type": "string"},
    }
    return {
        "type": "object",
        "properties": properties,
        "required": list(properties),
        "additionalProperties": False,
    }


def _nullable_object(properties: dict[str, Any]) -> dict[str, Any]:
    return {
        "anyOf": [
            {
                "type": "object",
                "properties": properties,
                "required": list(properties),
                "additionalProperties": False,
            },
            {"type": "null"},
        ]
    }


__all__ = ["build_extraction_response_schema"]
