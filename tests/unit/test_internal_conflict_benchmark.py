from __future__ import annotations

from benchmarks.internal.conflict_cases import load_conflict_development_cases


REQUIRED_TYPES = {
    "direct_contradiction",
    "explicit_correction",
    "supersession",
    "temporal_change",
    "compatible_facts",
    "mergeable",
    "clarification",
    "source_authority",
    "multi_service",
    "duplicate_delivery",
    "out_of_order",
    "conflict_chain",
    "reinforcement",
    "post_resolution_update",
    "missing_provenance",
    "equal_authority_recency",
    "partial_evidence",
    "transaction_retry",
}


def test_conflict_development_dataset_has_required_scenarios() -> None:
    cases = load_conflict_development_cases()
    assert len(cases) == 18
    assert len({case.id for case in cases}) == len(cases)
    assert {case.scenario_type for case in cases} == REQUIRED_TYPES


def test_conflict_cases_define_full_event_and_state_expectations() -> None:
    for case in load_conflict_development_cases():
        assert len(case.events) >= 2
        assert all(event.get("content") for event in case.events)
        assert "conflict" in case.expected
        assert case.expected.get("action")
        assert case.expected.get("active_winners") is not None


def test_conflict_dataset_is_development_only() -> None:
    assert "development" in str(load_conflict_development_cases.__defaults__[0])
    assert "holdout" not in str(load_conflict_development_cases.__defaults__[0])
