from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class DomainMemoryProjection:
    """A portable fact derived from a domain-specific memory record."""

    projection_key: str
    content: str
    category: str
    importance_score: float
    confidence: float
    source_domain: str
    source_domain_record_id: str
    source_field: str
    portability: str = "cross_agent"
    sensitivity: str = "normal"


@dataclass(frozen=True)
class DomainProjectionResult:
    created: int = 0
    updated: int = 0
    unchanged: int = 0
    skipped: int = 0
    skipped_reason: str | None = None


__all__ = ["DomainMemoryProjection", "DomainProjectionResult"]
