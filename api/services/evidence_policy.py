from __future__ import annotations

import re
from dataclasses import dataclass
from enum import IntEnum
from typing import Any


class EvidenceAuthority(IntEnum):
    """Server-owned authority ceilings for conversational evidence."""

    UNTRUSTED_CONTEXT = 10
    CLIENT_ASSERTION = 20
    LEGACY_CONVERSATION = 50
    APPLICATION_ATTESTED = 60
    MEMORYOS_ATTESTED = 100


@dataclass(frozen=True)
class EvidenceDecision:
    accepted: bool
    authority: EvidenceAuthority
    reason: str
    user_turn_indexes: tuple[int, ...] = ()
    proposal_turn_index: int | None = None
    proposal_id: str | None = None
    proposal_group_id: str | None = None
    proposal_ordinal: int | None = None
    review_required: bool = False


def authority_for_submission(
    *,
    mode: str,
    has_memoryos_envelope: bool = False,
    writer_capabilities: set[str] | None = None,
) -> EvidenceAuthority:
    """Resolve authority from server-observed facts, never caller metadata."""

    if has_memoryos_envelope:
        return EvidenceAuthority.MEMORYOS_ATTESTED
    if "user_evidence:attest" in (writer_capabilities or set()):
        return EvidenceAuthority.APPLICATION_ATTESTED
    if mode == "client_assertion":
        return EvidenceAuthority.CLIENT_ASSERTION
    # Preserve the current API authority while the signed evidence-envelope
    # endpoint is introduced. This is intentionally below attested evidence.
    return EvidenceAuthority.LEGACY_CONVERSATION


def validate_conversational_evidence(
    *,
    messages: list[dict[str, Any]],
    evidence_turns: Any,
    evidence_relation: Any,
    proposal_turn: Any,
    visible_turn_indexes: set[int] | None = None,
    proposal_confirmation_enabled: bool = False,
    active_proposals: list[dict[str, Any]] | None = None,
) -> EvidenceDecision:
    """Verify model citations against typed roles, ordering, and proposal scope."""

    if not isinstance(evidence_turns, list) or not evidence_turns:
        return EvidenceDecision(False, EvidenceAuthority.CLIENT_ASSERTION, "missing_evidence")
    indexes = tuple(
        sorted(
            {
                value
                for value in evidence_turns
                if isinstance(value, int)
                and not isinstance(value, bool)
                and 0 <= value < len(messages)
            }
        )
    )
    if len(indexes) != len(evidence_turns):
        return EvidenceDecision(False, EvidenceAuthority.CLIENT_ASSERTION, "invalid_evidence_turn")
    if visible_turn_indexes is not None and any(index not in visible_turn_indexes for index in indexes):
        return EvidenceDecision(False, EvidenceAuthority.CLIENT_ASSERTION, "evidence_not_visible_to_model")

    cited_user_indexes = tuple(
        index
        for index in indexes
        if _canonical_role(messages[index]) == "user"
        and _source_kind(messages[index]) in {"direct_user_input", "client_assertion"}
    )

    relation = str(evidence_relation or "direct_user_statement")
    if relation == "direct_user_statement":
        if not cited_user_indexes:
            return EvidenceDecision(
                False, EvidenceAuthority.CLIENT_ASSERTION, "no_user_evidence"
            )
        return EvidenceDecision(
            True,
            EvidenceAuthority.CLIENT_ASSERTION,
            "direct_user_statement",
            user_turn_indexes=cited_user_indexes,
        )
    if relation != "user_confirmed_assistant_proposal":
        return EvidenceDecision(False, EvidenceAuthority.CLIENT_ASSERTION, "unsupported_relation")
    if not proposal_confirmation_enabled:
        return EvidenceDecision(False, EvidenceAuthority.CLIENT_ASSERTION, "semantic_confirmation_not_enabled")
    if not isinstance(proposal_turn, int) or isinstance(proposal_turn, bool):
        return EvidenceDecision(False, EvidenceAuthority.CLIENT_ASSERTION, "missing_proposal_turn")
    if not 0 <= proposal_turn < len(messages):
        return EvidenceDecision(False, EvidenceAuthority.CLIENT_ASSERTION, "invalid_proposal_turn")
    if visible_turn_indexes is not None and proposal_turn not in visible_turn_indexes:
        return EvidenceDecision(False, EvidenceAuthority.CLIENT_ASSERTION, "proposal_not_visible_to_model")
    if _canonical_role(messages[proposal_turn]) != "assistant":
        return EvidenceDecision(False, EvidenceAuthority.CLIENT_ASSERTION, "proposal_not_assistant")
    if _source_kind(messages[proposal_turn]) != "assistant_output":
        return EvidenceDecision(False, EvidenceAuthority.CLIENT_ASSERTION, "proposal_not_assistant_output")
    if not bool(messages[proposal_turn].get("is_memory_proposal")):
        return EvidenceDecision(False, EvidenceAuthority.CLIENT_ASSERTION, "proposal_not_registered")
    eligible_user_indexes = tuple(
        index
        for index, message in enumerate(messages)
        if index > proposal_turn
        and _canonical_role(message) == "user"
        and _source_kind(message) in {"direct_user_input", "client_assertion"}
        and (visible_turn_indexes is None or index in visible_turn_indexes)
    )
    user_indexes = tuple(
        index for index in cited_user_indexes if index > proposal_turn
    ) or eligible_user_indexes[-1:]
    if not user_indexes:
        return EvidenceDecision(
            False,
            EvidenceAuthority.CLIENT_ASSERTION,
            "no_user_evidence",
        )
    if max(user_indexes) - proposal_turn > 12:
        return EvidenceDecision(
            False,
            EvidenceAuthority.CLIENT_ASSERTION,
            "proposal_outside_turn_window",
        )

    active = list(active_proposals or [])
    target = next(
        (
            proposal
            for proposal in active
            if int(proposal.get("turn_index", -1)) == proposal_turn
            and str(proposal.get("turn_id") or "")
            == str(messages[proposal_turn].get("turn_id") or "")
            and str(proposal.get("content_sha256") or "")
            == str(messages[proposal_turn].get("turn_content_sha256") or "")
        ),
        None,
    )
    if target is None:
        return EvidenceDecision(False, EvidenceAuthority.CLIENT_ASSERTION, "proposal_not_active")

    latest_user_text = str(messages[max(user_indexes)].get("content") or "")
    if _explicit_confirmation_denial(latest_user_text):
        return EvidenceDecision(
            False,
            EvidenceAuthority.CLIENT_ASSERTION,
            "explicit_confirmation_denied",
            user_turn_indexes=user_indexes,
            proposal_turn_index=proposal_turn,
        )
    referenced_ordinal = _explicit_proposal_ordinal(latest_user_text, active)
    if referenced_ordinal is not None and int(target.get("ordinal", -1)) != referenced_ordinal:
        return EvidenceDecision(
            False,
            EvidenceAuthority.CLIENT_ASSERTION,
            "proposal_reference_mismatch",
            user_turn_indexes=user_indexes,
            proposal_turn_index=proposal_turn,
            review_required=True,
        )
    if referenced_ordinal is None and len(active) != 1:
        return EvidenceDecision(
            False,
            EvidenceAuthority.CLIENT_ASSERTION,
            "ambiguous_proposal_reference",
            user_turn_indexes=user_indexes,
            proposal_turn_index=proposal_turn,
            review_required=True,
        )

    return EvidenceDecision(
        True,
        EvidenceAuthority.CLIENT_ASSERTION,
        "verified_proposal_reference",
        user_turn_indexes=user_indexes,
        proposal_turn_index=proposal_turn,
        proposal_id=str(target.get("id") or "") or None,
        proposal_group_id=str(target.get("group_id") or "") or None,
        proposal_ordinal=int(target["ordinal"]) if target.get("ordinal") is not None else None,
    )


_CONFIRMATION_DENIAL_PATTERNS = (
    re.compile(r"[?？]\s*$"),
    re.compile(r"\b(?:no|nope|nah)\b", re.IGNORECASE),
    re.compile(r"\b(?:do\s+not|don't|dont)\s+(?:remember|save|store|keep)\b", re.IGNORECASE),
    re.compile(r"\b(?:not|never)\s+(?:agreeing|approve|remember|save|store|keep)\b", re.IGNORECASE),
    re.compile(r"\b(?:nahi|nahin)\b", re.IGNORECASE),
    re.compile(r"\bmat\s+(?:rakhna|rakho|banana|karo)\b", re.IGNORECASE),
    re.compile(r"(?:^|[\s,;.!?।])(?:नहीं|मत|गलत)(?=$|[\s,;.!?।])"),
)


def _explicit_confirmation_denial(user_text: str) -> bool:
    normalized = " ".join(user_text.split())
    return any(pattern.search(normalized) for pattern in _CONFIRMATION_DENIAL_PATTERNS)


_ORDINAL_WORDS = {
    "first": 1,
    "second": 2,
    "third": 3,
    "fourth": 4,
    "fifth": 5,
    "sixth": 6,
    "seventh": 7,
    "eighth": 8,
    "ninth": 9,
    "tenth": 10,
}


def _explicit_proposal_ordinal(
    user_text: str,
    active_proposals: list[dict[str, Any]],
) -> int | None:
    # Resolve only explicit list references; semantic binding stays with the extractor.
    normalized = " ".join(user_text.lower().split())
    if not normalized:
        return None
    for word, ordinal in _ORDINAL_WORDS.items():
        if re.search(
            rf"\b(?:the\s+{word}|{word}\s+(?:one|option|proposal|choice))\b",
            normalized,
        ):
            return ordinal
    multilingual_ordinals = (
        (r"\b(?:pehla|pahla|pehli|pehle)\s+wala\b", 1),
        (r"\b(?:doosra|dusra|doosri|dusri|doosre|dusre)\s+wala\b", 2),
        (r"(?:पहले|पहला|पहली)\s+(?:वाला|वाले|वाली|प्रस्ताव|विकल्प)", 1),
        (r"(?:दूसरे|दूसरा|दूसरी)\s+(?:वाला|वाले|वाली|प्रस्ताव|विकल्प)", 2),
    )
    for pattern, ordinal in multilingual_ordinals:
        if re.search(pattern, normalized):
            return ordinal
    numeric = re.search(
        r"(?:\b(?:option|proposal|choice)(?:\s+number)?|\bnumber|विकल्प|प्रस्ताव\s+संख्या)\s*#?\s*(\d{1,2})(?:st|nd|rd|th)?\b",
        normalized,
    )
    if numeric:
        return int(numeric.group(1))
    if re.search(
        r"(?:\b(?:the\s+(?:last|latter)|last\s+(?:one|option|proposal|choice|wala))\b|अंतिम\s+(?:वाला|वाले|वाली))",
        normalized,
    ):
        ordinals = [
            int(item["ordinal"])
            for item in active_proposals
            if item.get("ordinal") is not None
        ]
        return max(ordinals) if ordinals else None
    if re.search(r"\b(?:the\s+former|former\s+(?:one|option|proposal|choice))\b", normalized):
        return 1
    return None


def _canonical_role(message: dict[str, Any]) -> str:
    return str(message.get("role") or "").strip().lower()


def _source_kind(message: dict[str, Any]) -> str:
    explicit = str(message.get("source_kind") or "").strip().lower()
    if explicit:
        return explicit
    return "direct_user_input" if _canonical_role(message) == "user" else "assistant_output"


__all__ = [
    "EvidenceAuthority",
    "EvidenceDecision",
    "authority_for_submission",
    "validate_conversational_evidence",
]
