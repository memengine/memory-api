from benchmarks.internal.lifecycle_provenance_cases import DATASET
from benchmarks.internal.lifecycle_provenance_cases import load_lifecycle_provenance_cases


def test_lifecycle_provenance_pack_is_development_only_and_frozen() -> None:
    cases = load_lifecycle_provenance_cases()
    assert "development" in DATASET.parts
    assert "holdout" not in DATASET.parts
    assert len(cases) == 25
    assert len({case.id for case in cases}) == len(cases)


def test_lifecycle_provenance_pack_covers_required_boundaries() -> None:
    cases = load_lifecycle_provenance_cases()
    areas = {case.area for case in cases}
    levels = {case.level for case in cases}
    required = {
        "source_evidence", "persistence_versioning", "conflict_winner",
        "retry_idempotency", "outbox_vector_consistency", "api_readback",
        "transaction_integrity", "isolation_retention",
    }
    assert required <= areas
    assert {"production_path_unit", "integration", "postgresql"} <= levels
    assert all(case.test_node.startswith("tests/") for case in cases)
