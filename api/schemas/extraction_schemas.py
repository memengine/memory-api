from __future__ import annotations

from dataclasses import dataclass
from dataclasses import field

from api.schemas.memory_schemas import ExtractedMemory


@dataclass(slots=True)
class ExtractionResult:
    memories_extracted: int
    memories_filtered: int
    conflicts_resolved: int
    nothing_to_extract: bool
    tokens_used: int
    provider_used: str
    job_id: str
    memories_to_store: list[ExtractedMemory] = field(default_factory=list)


__all__ = ["ExtractionResult"]
