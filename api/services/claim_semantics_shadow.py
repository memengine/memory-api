from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from datetime import datetime
from typing import Any

SCHEMA_VERSION = "claim-semantics-shadow-v1"
MAX_OBSERVATIONS = 8
MAX_PREDICATE_LENGTH = 120
MAX_VALUE_LENGTH = 500
MAX_EVIDENCE_QUOTE_LENGTH = 240
ALLOWED_CATEGORIES = frozenset(
    {"preference", "fact", "goal", "procedure", "relationship", "expertise"}
)
ALLOWED_SPEECH_ACTS = frozenset(
    {"assertion", "correction", "retraction", "uncertain_change", "reaffirmation"}
)
ALLOWED_CERTAINTY = frozenset({"certain", "uncertain"})
ALLOWED_TEMPORAL_KINDS = frozenset({"permanent", "bounded", "unknown"})
PREDICATE_PATTERN = re.compile(r"^[a-z][a-z0-9]*(?:[._-][a-z0-9]+)*$")


def observe_claim_semantics(
    raw_content: str,
    *,
    messages: list[dict[str, Any]],
    visible_turn_indexes: set[int],
    source_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Validate model-produced claim semantics without affecting active memory writes."""

    result: dict[str, Any] = {
        "enabled": True,
        "schema_version": SCHEMA_VERSION,
        "model_returned": 0,
        "accepted": 0,
        "rejected": 0,
        "rejection_counts": {},
        "observations": [],
    }
    try:
        payload = json.loads(raw_content or "{}")
    except json.JSONDecodeError:
        _reject(result, "invalid_json")
        return result
    if not isinstance(payload, dict):
        _reject(result, "invalid_root")
        return result

    raw_observations = payload.get("claim_semantics_shadow") or []
    if not isinstance(raw_observations, list):
        _reject(result, "invalid_observation_list")
        return result

    result["model_returned"] = len(raw_observations)
    if len(raw_observations) > MAX_OBSERVATIONS:
        _reject(
            result,
            "observation_limit_exceeded",
            len(raw_observations) - MAX_OBSERVATIONS,
        )
        raw_observations = raw_observations[:MAX_OBSERVATIONS]

    memory_count = len(payload.get("memories") or []) if isinstance(payload.get("memories"), list) else 0
    for raw in raw_observations:
        observation, reason = _validate_observation(
            raw,
            messages=messages,
            visible_turn_indexes=visible_turn_indexes,
            memory_count=memory_count,
            source_context=source_context,
        )
        if observation is None:
            _reject(result, reason or "invalid_observation")
            continue
        result["observations"].append(observation)
        result["accepted"] += 1
    return result


def disabled_claim_semantics_observation() -> dict[str, Any]:
    return {
        "enabled": False,
        "schema_version": SCHEMA_VERSION,
        "model_returned": 0,
        "accepted": 0,
        "rejected": 0,
        "rejection_counts": {},
        "observations": [],
    }


def _validate_observation(
    raw: Any,
    *,
    messages: list[dict[str, Any]],
    visible_turn_indexes: set[int],
    memory_count: int,
    source_context: dict[str, Any] | None,
) -> tuple[dict[str, Any] | None, str | None]:
    if not isinstance(raw, dict):
        return None, "invalid_shape"

    text_fields = (
        "predicate",
        "value",
        "category",
        "speech_act",
        "certainty",
        "temporal_kind",
    )
    for field in text_fields:
        if not isinstance(raw.get(field), str):
            return None, f"invalid_{field}_type"
    predicate = raw["predicate"].strip().lower()
    value = raw["value"].strip()
    category = raw["category"].strip().lower()
    speech_act = raw["speech_act"].strip().lower()
    certainty = raw["certainty"].strip().lower()
    temporal_kind = raw["temporal_kind"].strip().lower()
    if (
        not predicate
        or len(predicate) > MAX_PREDICATE_LENGTH
        or not PREDICATE_PATTERN.fullmatch(predicate)
    ):
        return None, "invalid_predicate"
    if not value or len(value) > MAX_VALUE_LENGTH:
        return None, "invalid_value"
    if category not in ALLOWED_CATEGORIES:
        return None, "invalid_category"
    if speech_act not in ALLOWED_SPEECH_ACTS:
        return None, "invalid_speech_act"
    if certainty not in ALLOWED_CERTAINTY:
        return None, "invalid_certainty"
    if temporal_kind not in ALLOWED_TEMPORAL_KINDS:
        return None, "invalid_temporal_kind"

    memory_index = raw.get("memory_index")
    if memory_index is not None and (
        isinstance(memory_index, bool)
        or not isinstance(memory_index, int)
        or memory_index < 0
        or memory_index >= memory_count
    ):
        return None, "invalid_memory_index"

    effective_from, from_error = _normalize_datetime(raw.get("effective_from"))
    if from_error:
        return None, "invalid_effective_from"
    effective_until, until_error = _normalize_datetime(raw.get("effective_until"))
    if until_error:
        return None, "invalid_effective_until"
    if temporal_kind == "bounded" and effective_until is None:
        return None, "bounded_without_end"
    if (
        effective_from
        and effective_until
        and datetime.fromisoformat(effective_until)
        <= datetime.fromisoformat(effective_from)
    ):
        return None, "invalid_temporal_order"

    evidence_turns, evidence_error = _validated_evidence_turns(
        raw.get("evidence_turns"),
        messages=messages,
        visible_turn_indexes=visible_turn_indexes,
        source_context=source_context,
    )
    if evidence_error:
        return None, evidence_error
    evidence_quote, quote_error = _validated_evidence_quote(
        raw.get("evidence_quote"),
        messages=messages,
        evidence_turns=evidence_turns,
        source_context=source_context,
    )
    if quote_error:
        return None, quote_error

    value_digest = hashlib.sha256(value.encode("utf-8")).hexdigest()
    return (
        {
            "memory_index": memory_index,
            "category": category,
            "predicate": predicate,
            "value_sha256": value_digest,
            "value_length": len(value),
            "speech_act": speech_act,
            "certainty": certainty,
            "temporal_kind": temporal_kind,
            "effective_from": effective_from,
            "effective_until": effective_until,
            "evidence_turns": evidence_turns,
            "evidence_source": "authenticated_service" if source_context else "user_turns",
            "evidence_quote_sha256": (
                hashlib.sha256(evidence_quote.encode("utf-8")).hexdigest()
                if evidence_quote
                else None
            ),
            "evidence_quote_length": len(evidence_quote),
        },
        None,
    )


def _validated_evidence_turns(
    value: Any,
    *,
    messages: list[dict[str, Any]],
    visible_turn_indexes: set[int],
    source_context: dict[str, Any] | None,
) -> tuple[list[int], str | None]:
    if source_context:
        return [], None
    if not isinstance(value, list) or not value:
        return [], "missing_evidence_turns"
    validated: list[int] = []
    for index in value:
        if isinstance(index, bool) or not isinstance(index, int):
            return [], "invalid_evidence_turn"
        if index in validated:
            continue
        if index < 0 or index >= len(messages) or index not in visible_turn_indexes:
            return [], "unavailable_evidence_turn"
        if str(messages[index].get("role") or "").strip().lower() != "user":
            return [], "non_user_evidence_turn"
        validated.append(index)
    return validated, None


def _normalize_datetime(value: Any) -> tuple[str | None, bool]:
    if value is None or value == "":
        return None, False
    if not isinstance(value, str):
        return None, True
    candidate = value.strip()
    if not candidate:
        return None, False
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError:
        return None, True
    if parsed.tzinfo is None:
        return None, True
    return parsed.isoformat(), False


def _validated_evidence_quote(
    value: Any,
    *,
    messages: list[dict[str, Any]],
    evidence_turns: list[int],
    source_context: dict[str, Any] | None,
) -> tuple[str, str | None]:
    if source_context:
        return "", None
    if not isinstance(value, str):
        return "", "missing_evidence_quote"
    quote = " ".join(unicodedata.normalize("NFKC", value).casefold().split())
    if len(quote) < 2 or len(quote) > MAX_EVIDENCE_QUOTE_LENGTH:
        return "", "invalid_evidence_quote"
    for index in evidence_turns:
        content = " ".join(
            unicodedata.normalize(
                "NFKC", str(messages[index].get("content") or "")
            ).casefold().split()
        )
        if quote in content:
            return quote, None
    return "", "evidence_quote_not_in_user_turn"


def _reject(result: dict[str, Any], reason: str, count: int = 1) -> None:
    result["rejected"] += count
    rejection_counts = result["rejection_counts"]
    rejection_counts[reason] = rejection_counts.get(reason, 0) + count


__all__ = [
    "SCHEMA_VERSION",
    "disabled_claim_semantics_observation",
    "observe_claim_semantics",
]
