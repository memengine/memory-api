from __future__ import annotations

from datetime import UTC, datetime

from api.services.retriever import MemoryResult, RetrieverService


NOW = datetime(2026, 8, 12, 12, 0, tzinfo=UTC)


def _result(
    *, effective_from: str | None = None, effective_until: str | None = None
) -> MemoryResult:
    return MemoryResult(
        id=f"{effective_from}-{effective_until}", content="memory", category="fact",
        importance_score=5.0, confidence_score=0.9, semantic_score=0.8,
        recency_score=1.0, final_score=0.8, agent_id=None,
        previous_version_id=None, last_accessed_at=None,
        effective_from=effective_from, effective_until=effective_until,
    )


def test_current_validity_preserves_unbounded_and_current_memories() -> None:
    results = [
        _result(),
        _result(effective_from="2026-08-01T00:00:00Z"),
        _result(effective_until="2026-09-01T00:00:00Z"),
        _result(
            effective_from="2026-08-01T00:00:00Z",
            effective_until="2026-09-01T00:00:00Z",
        ),
    ]
    assert RetrieverService._filter_current_results(results, now=NOW) == results


def test_current_validity_removes_future_and_expired_memories() -> None:
    valid = _result()
    future = _result(effective_from="2026-08-13T00:00:00Z")
    expired = _result(effective_until="2026-08-12T12:00:00Z")

    assert RetrieverService._filter_current_results(
        [future, valid, expired], now=NOW
    ) == [valid]


def test_qdrant_payload_validity_uses_same_boundary_contract() -> None:
    assert RetrieverService._payload_is_current({}, now=NOW) is True
    assert RetrieverService._payload_is_current(
        {"effective_from": "2026-08-13T00:00:00Z"}, now=NOW
    ) is False
    assert RetrieverService._payload_is_current(
        {"effective_until": "2026-08-12T12:00:00Z"}, now=NOW
    ) is False


def test_end_boundary_is_exclusive_and_start_boundary_is_inclusive() -> None:
    assert RetrieverService._is_valid_at(
        "2026-08-12T12:00:00Z", None, NOW
    ) is True
    assert RetrieverService._is_valid_at(
        None, "2026-08-12T12:00:00Z", NOW
    ) is False
