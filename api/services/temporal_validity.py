from __future__ import annotations

from datetime import UTC, datetime
from typing import Any


def temporal_validity_from_provenance(
    provenance: dict[str, Any] | None,
) -> tuple[datetime | None, datetime | None]:
    """Read an immutable validity interval from the source provenance scope."""
    scope = dict((provenance or {}).get("scope") or {})
    effective_from = _parse_datetime(scope.get("effective_from"), "effective_from")
    effective_until = _parse_datetime(scope.get("effective_until"), "effective_until")
    if (
        effective_from is not None
        and effective_until is not None
        and effective_until <= effective_from
    ):
        raise ValueError("effective_until must be later than effective_from")
    return effective_from, effective_until


def _parse_datetime(value: Any, field: str) -> datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        normalized = value.strip().replace("Z", "+00:00")
        try:
            parsed = datetime.fromisoformat(normalized)
        except ValueError as exc:
            raise ValueError(f"{field} must be an ISO-8601 datetime") from exc
    else:
        raise ValueError(f"{field} must be an ISO-8601 datetime")
    if parsed.tzinfo is None:
        raise ValueError(f"{field} must include a timezone")
    return parsed.astimezone(UTC)
