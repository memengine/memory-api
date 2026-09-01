from benchmarks.internal.temporal_cases import DATASET, load_temporal_cases


def test_temporal_pack_is_frozen_development_only() -> None:
    cases = load_temporal_cases()
    assert "development" in DATASET.parts and "holdout" not in DATASET.parts
    assert len(cases) == 18
    assert len({case.id for case in cases}) == 18


def test_temporal_pack_covers_required_areas() -> None:
    areas = {case.area for case in load_temporal_cases()}
    assert {"validity_schema", "event_time_authority", "conflict_versioning", "expiration_lifecycle",
            "temporal_retrieval", "timezone_recurring", "vector_integration"} <= areas
