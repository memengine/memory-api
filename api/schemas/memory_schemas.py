from __future__ import annotations

from typing import Any
from typing import Literal

from pydantic import BaseModel
from pydantic import Field


class ExtractedMemory(BaseModel):
    content: str = Field(min_length=1)
    category: Literal["preference", "fact", "goal", "procedure", "relationship", "expertise"]
    importance_score: float = Field(ge=1.0, le=10.0)
    confidence: float = Field(ge=0.0, le=1.0)
    expiry: Literal["permanent", "temporary"]
    reasoning: str = Field(min_length=1)
    # Internal-only evidence produced by the backend validation gate. It is
    # deliberately not part of the LLM response schema or a public write API.
    validated_evidence: dict[str, Any] = Field(default_factory=dict, exclude=True)


class ExtractionResponseSchema(BaseModel):
    memories: list[ExtractedMemory] = Field(default_factory=list)
