from __future__ import annotations

import logging
import re
from typing import Any


LOGGER = logging.getLogger(__name__)
REDACTED = "[REDACTED]"

COMMON_PATTERNS = (
    re.compile(r"\b\d{4}\s\d{4}\s\d{4}\b"),
    re.compile(r"\b[A-Z]{5}[0-9]{4}[A-Z]\b"),
    re.compile(r"\b\d{4}[\s-]\d{4}[\s-]\d{4}[\s-]\d{4}\b"),
    re.compile(r"\b(?:otp|code)\s*(?:is|:)?\s*\d{4,8}\b", re.IGNORECASE),
)
BANKING_ACCOUNT_PATTERN = re.compile(r"\b\d{9,18}\b")
PASSPORT_PATTERN = re.compile(r"\b[A-Z][0-9]{7}\b")
IMEI_PATTERN = re.compile(r"\b\d{15}\b")


def redact_support_pii(value: Any, support_type: str | None = None) -> Any:
    redacted, count = _redact(value, support_type=support_type, key_path="")
    if count:
        LOGGER.warning(
            "support_pii_redacted",
            extra={"event": "support_pii_redacted", "support_type": support_type, "redactions_count": count},
        )
    return redacted


def count_redactions(value: Any, support_type: str | None = None) -> int:
    _, count = _redact(value, support_type=support_type, key_path="")
    return count


def _redact(value: Any, *, support_type: str | None, key_path: str) -> tuple[Any, int]:
    if isinstance(value, dict):
        output: dict[str, Any] = {}
        total = 0
        for key, item in value.items():
            next_value, count = _redact(item, support_type=support_type, key_path=f"{key_path}.{key}".strip("."))
            output[key] = next_value
            total += count
        return output, total
    if isinstance(value, list):
        output_list = []
        total = 0
        for index, item in enumerate(value):
            next_value, count = _redact(item, support_type=support_type, key_path=f"{key_path}.{index}".strip("."))
            output_list.append(next_value)
            total += count
        return output_list, total
    if isinstance(value, str):
        return _redact_string(value, support_type=support_type, key_path=key_path)
    return value, 0


def _redact_string(value: str, *, support_type: str | None, key_path: str) -> tuple[str, int]:
    if _is_explicit_safe_last4(key_path):
        return value, 0

    redacted = value
    count = 0
    for pattern in COMMON_PATTERNS:
        redacted, replacements = pattern.subn(REDACTED, redacted)
        count += replacements

    if support_type == "banking_fintech":
        redacted, replacements = BANKING_ACCOUNT_PATTERN.subn(REDACTED, redacted)
        count += replacements
    if support_type == "travel":
        redacted, replacements = PASSPORT_PATTERN.subn(REDACTED, redacted)
        count += replacements
    if support_type == "telecom" and not _is_explicit_safe_last4(key_path):
        redacted, replacements = IMEI_PATTERN.subn(REDACTED, redacted)
        count += replacements

    return redacted, count


def _is_explicit_safe_last4(key_path: str) -> bool:
    normalized = key_path.lower()
    return "last4" in normalized or "last_4" in normalized or "last_four" in normalized


__all__ = ["count_redactions", "redact_support_pii"]
