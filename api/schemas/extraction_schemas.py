from __future__ import annotations

from dataclasses import dataclass
from dataclasses import field
from typing import Any

from api.schemas.memory_schemas import ExtractedMemory


@dataclass(slots=True)
class PendingExtractedMemory:
    content: str
    category: str
    importance_score: float
    confidence: float
    reasoning: str
    candidate_reason: str = "confidence_below_store_threshold"


@dataclass(slots=True)
class ExtractionResult:
    memories_extracted: int
    memories_filtered: int
    pending_candidates_count: int
    conflicts_resolved: int
    nothing_to_extract: bool
    tokens_used: int
    provider_used: str
    job_id: str
    memories_to_store: list[ExtractedMemory] = field(default_factory=list)
    pending_candidates: list[PendingExtractedMemory] = field(default_factory=list)
    extraction_metadata: dict[str, Any] = field(default_factory=dict)


__all__ = ["ExtractionResult", "PendingExtractedMemory"]
