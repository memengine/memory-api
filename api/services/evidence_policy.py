from __future__ import annotations

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

    user_indexes = tuple(
        index
        for index in indexes
        if _canonical_role(messages[index]) == "user"
        and _source_kind(messages[index]) in {"direct_user_input", "client_assertion"}
    )
    if not user_indexes:
        return EvidenceDecision(False, EvidenceAuthority.CLIENT_ASSERTION, "no_user_evidence")

    relation = str(evidence_relation or "direct_user_statement")
    if relation == "direct_user_statement":
        return EvidenceDecision(
            True,
            EvidenceAuthority.CLIENT_ASSERTION,
            "direct_user_statement",
            user_turn_indexes=user_indexes,
        )
    if relation != "user_confirmed_assistant_proposal":
        return EvidenceDecision(False, EvidenceAuthority.CLIENT_ASSERTION, "unsupported_relation")
    if not isinstance(proposal_turn, int) or isinstance(proposal_turn, bool):
        return EvidenceDecision(False, EvidenceAuthority.CLIENT_ASSERTION, "missing_proposal_turn")
    if proposal_turn not in indexes or not 0 <= proposal_turn < len(messages):
        return EvidenceDecision(False, EvidenceAuthority.CLIENT_ASSERTION, "uncited_proposal_turn")
    if _canonical_role(messages[proposal_turn]) != "assistant":
        return EvidenceDecision(False, EvidenceAuthority.CLIENT_ASSERTION, "proposal_not_assistant")
    if any(user_index <= proposal_turn for user_index in user_indexes):
        return EvidenceDecision(False, EvidenceAuthority.CLIENT_ASSERTION, "confirmation_precedes_proposal")
    if max(user_indexes) - proposal_turn > 12:
        return EvidenceDecision(False, EvidenceAuthority.CLIENT_ASSERTION, "proposal_outside_turn_window")

    return EvidenceDecision(
        True,
        EvidenceAuthority.CLIENT_ASSERTION,
        "verified_proposal_reference",
        user_turn_indexes=user_indexes,
        proposal_turn_index=proposal_turn,
    )


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
