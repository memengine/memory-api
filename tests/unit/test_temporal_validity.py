from datetime import UTC, datetime

import pytest

from api.services.temporal_validity import temporal_validity_from_provenance


def test_temporal_validity_reads_source_scope_and_normalizes_utc() -> None:
    effective_from, effective_until = temporal_validity_from_provenance(
        {
            "scope": {
                "effective_from": "2026-08-12T09:30:00+05:30",
                "effective_until": "2026-08-13T09:30:00+05:30",
            }
        }
    )

    assert effective_from == datetime(2026, 8, 12, 4, 0, tzinfo=UTC)
    assert effective_until == datetime(2026, 8, 13, 4, 0, tzinfo=UTC)


def test_temporal_validity_allows_open_intervals_and_legacy_sources() -> None:
    assert temporal_validity_from_provenance(None) == (None, None)
    assert temporal_validity_from_provenance(
        {"scope": {"effective_from": "2026-08-12T00:00:00Z"}}
    ) == (datetime(2026, 8, 12, tzinfo=UTC), None)


@pytest.mark.parametrize(
    ("effective_from", "effective_until"),
    [
        ("2026-08-12T00:00:00Z", "2026-08-12T00:00:00Z"),
        ("2026-08-13T00:00:00Z", "2026-08-12T00:00:00Z"),
    ],
)
def test_temporal_validity_rejects_non_positive_closed_intervals(
    effective_from: str, effective_until: str
) -> None:
    with pytest.raises(ValueError, match="later than"):
        temporal_validity_from_provenance(
            {
                "scope": {
                    "effective_from": effective_from,
                    "effective_until": effective_until,
                }
            }
        )


def test_temporal_validity_rejects_naive_datetimes() -> None:
    with pytest.raises(ValueError, match="timezone"):
        temporal_validity_from_provenance(
            {"scope": {"effective_from": "2026-08-12T00:00:00"}}
        )
