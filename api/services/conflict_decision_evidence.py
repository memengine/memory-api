from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from typing import Literal


DecisionAction = Literal[
    "UPDATE",
    "REJECT",
    "MERGE",
    "KEEP_BOTH",
    "USER_REVIEW",
    "TENANT_REVIEW",
    "IGNORE",
]

DecisionLevel = Literal[
    "automatic",
    "personal_truth",
    "organisational_truth",
    "manual",
]


@dataclass(slots=True)
class ConflictDecisionEvidence:
    """Small, persisted explanation for why a conflict moved the way it did."""

    action: DecisionAction
    decision_level: DecisionLevel
    reason_codes: list[str]
    explanation: str
    confidence: float | None = None
    winner_source: str | None = None
    details: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "action": self.action,
            "decision_level": self.decision_level,
            "reason_codes": sorted(set(self.reason_codes)),
            "explanation": self.explanation,
            "confidence": self.confidence,
            "winner_source": self.winner_source,
            "details": self.details or {},
        }


def empty_decision_evidence() -> dict[str, Any]:
    return {}


def automatic_evidence(
    *,
    action: DecisionAction,
    reason_codes: list[str],
    explanation: str,
    confidence: float | None = None,
    winner_source: str | None = None,
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return ConflictDecisionEvidence(
        action=action,
        decision_level="automatic",
        reason_codes=reason_codes,
        explanation=explanation,
        confidence=confidence,
        winner_source=winner_source,
        details=details,
    ).to_dict()


def review_evidence(
    *,
    action: Literal["USER_REVIEW", "TENANT_REVIEW"],
    reason_codes: list[str],
    explanation: str,
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return ConflictDecisionEvidence(
        action=action,
        decision_level="personal_truth"
        if action == "USER_REVIEW"
        else "organisational_truth",
        reason_codes=reason_codes,
        explanation=explanation,
        confidence=None,
        winner_source=None,
        details=details,
    ).to_dict()


def manual_evidence(
    *,
    action: DecisionAction,
    reason_codes: list[str],
    explanation: str,
    winner_source: str | None = None,
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return ConflictDecisionEvidence(
        action=action,
        decision_level="manual",
        reason_codes=reason_codes,
        explanation=explanation,
        confidence=1.0,
        winner_source=winner_source,
        details=details,
    ).to_dict()
