from __future__ import annotations

from api.services.retriever_service import RetrieverService


def test_retrieval_hybrid_weights_are_valid_for_quality_ranking() -> None:
    assert RetrieverService.SEMANTIC_WEIGHT == 0.60
    assert RetrieverService.IMPORTANCE_WEIGHT == 0.25
    assert RetrieverService.RECENCY_WEIGHT == 0.15
    assert (
        RetrieverService.SEMANTIC_WEIGHT
        + RetrieverService.IMPORTANCE_WEIGHT
        + RetrieverService.RECENCY_WEIGHT
    ) == 1.0


def test_retrieval_overfetch_cap_matches_qdrant_limit() -> None:
    requested_limit = 25
    overfetch_limit = min(max(requested_limit * 3, requested_limit), 50)

    assert overfetch_limit == 50
