from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from api.services.support.support_schema import ALLOWED_SUPPORT_TYPES


@dataclass(slots=True)
class SupportTypeDetection:
    support_type: str
    confidence: float
    scores: dict[str, int]
    matched_signals: dict[str, list[str]]
    source: str


class SupportTypeDetector:
    DETECTION_SIGNALS: dict[str, tuple[str, ...]] = {
        "saas": (
            "workspace", "subscription", "plan", "api", "integration", "webhook", "dashboard",
            "login", "seat", "teammate", "billing", "feature request", "bug", "onboarding",
            "slack", "notion", "github", "zapier", "intercom", "crisp",
        ),
        "ecommerce": (
            "order", "delivery", "return", "refund", "product", "shipment", "seller", "cart",
            "tracking", "dispatch", "warehouse", "amazon", "flipkart", "meesho", "myntra",
        ),
        "banking_fintech": (
            "account", "bank", "transaction", "transfer", "upi", "neft", "imps", "credit card",
            "debit card", "loan", "emi", "kyc", "otp", "fraud", "block card", "cheque",
            "savings", "current", "fd", "mutual fund",
        ),
        "travel": (
            "flight", "train", "hotel", "booking", "pnr", "ticket", "cancel", "reschedule",
            "seat", "luggage", "boarding", "bus", "cab", "irctc", "indigo", "air india",
            "makemytrip", "goibibo", "loyalty", "miles",
        ),
        "telecom": (
            "sim", "network", "recharge", "plan", "data", "call", "internet", "signal",
            "port", "jio", "airtel", "vi", "bsnl", "roaming", "balance", "number", "imei",
        ),
        "edtech_support": (
            "course", "video", "certificate", "access", "password", "login", "subscription",
            "batch", "class", "recorded", "live session", "udemy", "coursera", "unacademy",
            "byjus", "payment failed", "course not opening",
        ),
        "general_info": (
            "how does", "what is", "pricing", "plans", "features", "compare", "difference",
            "demo", "trial", "signup", "about", "documentation", "api", "integration",
        ),
    }
    TIE_BREAK_PRIORITY = (
        "banking_fintech",
        "ecommerce",
        "travel",
        "telecom",
        "saas",
        "edtech_support",
        "general_info",
    )

    def detect(self, messages: list[dict[str, Any]], tenant_configured_type: str | None = None) -> str:
        return self.detect_result(messages, tenant_configured_type=tenant_configured_type).support_type

    def detect_result(
        self,
        messages: list[dict[str, Any]],
        tenant_configured_type: str | None = None,
        allowed_support_types: list[str] | None = None,
    ) -> SupportTypeDetection:
        if tenant_configured_type in ALLOWED_SUPPORT_TYPES:
            return SupportTypeDetection(
                support_type=str(tenant_configured_type),
                confidence=1.0,
                scores={str(tenant_configured_type): 999},
                matched_signals={str(tenant_configured_type): ["tenant_configured"]},
                source="tenant_configured",
            )

        allowed = _normalize_allowed_types(allowed_support_types)
        matched = self.explain_detection(messages, allowed_support_types=allowed)
        scores = {support_type: len(signals) for support_type, signals in matched.items()}
        best_score = max(scores.values(), default=0)
        if best_score <= 0:
            fallback = "general_info" if "general_info" in allowed else allowed[0]
            return SupportTypeDetection(fallback, 0.35, scores, matched, "detected")

        candidates = {support_type for support_type, score in scores.items() if score == best_score}
        support_type = next(item for item in self.TIE_BREAK_PRIORITY if item in candidates)
        return SupportTypeDetection(
            support_type=support_type,
            confidence=0.85 if best_score >= 2 else 0.55,
            scores=scores,
            matched_signals=matched,
            source="detected",
        )

    def explain_detection(
        self,
        messages: list[dict[str, Any]],
        allowed_support_types: list[str] | None = None,
    ) -> dict[str, list[str]]:
        allowed = set(_normalize_allowed_types(allowed_support_types))
        content = " ".join(str(message.get("content") or "").lower() for message in messages)
        return {
            support_type: [signal for signal in signals if signal.lower() in content]
            for support_type, signals in self.DETECTION_SIGNALS.items()
            if support_type in allowed
        }


def _normalize_allowed_types(allowed_support_types: list[str] | None) -> list[str]:
    normalized = [
        str(item)
        for item in (allowed_support_types or ALLOWED_SUPPORT_TYPES)
        if str(item) in ALLOWED_SUPPORT_TYPES
    ]
    return normalized or ["general_info"]


__all__ = ["SupportTypeDetection", "SupportTypeDetector"]
