from benchmarks.internal.lifecycle_activation_cases import load_lifecycle_activation_cases


def test_lifecycle_activation_dataset_is_frozen_and_broad() -> None:
    cases = load_lifecycle_activation_cases()
    assert len(cases) == 14
    assert len({case.id for case in cases}) == len(cases)
    assert {case.area for case in cases} == {
        "current_retrieval", "state_transition", "claim_consistency",
        "outbox_consistency", "cache_consistency", "scheduler", "decay",
        "historical_read",
    }
    assert any(case.level == "postgresql" for case in cases)
