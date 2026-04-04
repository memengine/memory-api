from __future__ import annotations

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


class ExtractionResponseSchema(BaseModel):
    memories: list[ExtractedMemory] = Field(default_factory=list)
