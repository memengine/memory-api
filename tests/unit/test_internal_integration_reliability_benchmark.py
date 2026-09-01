from benchmarks.internal.integration_reliability_cases import load_integration_reliability_cases


def test_reliability_dataset_has_unique_cases_and_required_areas() -> None:
    cases = load_integration_reliability_cases()
    assert len(cases) == 13
    assert len({case.id for case in cases}) == len(cases)
    assert {case.area for case in cases} == {
        "api_worker_persistence_readback", "duplicate_retry", "transaction_failure",
        "manual_conflict_resolution", "qdrant_outbox", "concurrency",
    }
    assert all("holdout" not in case.test_node.lower() for case in cases)


def test_reliability_dataset_uses_real_postgres_where_concurrency_requires_it() -> None:
    cases = {case.id: case for case in load_integration_reliability_cases()}
    assert cases["duplicate_source_delivery"].level == "postgresql"
    assert cases["concurrent_claim_update"].level == "postgresql"
