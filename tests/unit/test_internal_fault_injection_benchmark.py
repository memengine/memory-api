from benchmarks.internal.fault_injection_cases import DATASETS, load_fault_cases


def test_fault_pack_is_frozen_development_only_and_unique() -> None:
    cases = load_fault_cases()
    assert len(cases) == 32
    assert len({case.id for case in cases}) == len(cases)
    assert all("development" in path.parts and "holdout" not in str(path).lower() for path in DATASETS)


def test_fault_pack_covers_required_recovery_boundaries() -> None:
    cases = load_fault_cases()
    areas = {case.area for case in cases}
    metrics = {metric for case in cases for metric in case.validates}
    assert {"provider_failure","circuit_recovery","redis_degradation","qdrant_degradation","outbox_recovery","dead_letter_recovery","scheduler_recovery","privacy_retry","transaction_failure","concurrency"}.issubset(areas)
    assert {"provider_recovery","circuit_breaker_correctness","redis_failure_tolerance","qdrant_failure_tolerance","outbox_recovery","dead_letter_correctness","scheduler_catchup","duplicate_write_rate","single_winner_correctness","privacy_retry_correctness","tenant_isolation","data_loss_rate","data_leakage_rate"}.issubset(metrics)
