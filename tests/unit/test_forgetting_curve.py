from __future__ import annotations

from datetime import date
from datetime import timedelta

from api.services.edtech.forgetting_curve import compute_forgetting_stage
from api.services.edtech.forgetting_curve import days_since
from api.services.edtech.forgetting_curve import get_review_priority


def test_compute_forgetting_stage_boundaries() -> None:
    assert compute_forgetting_stage(0) == "fresh"
    assert compute_forgetting_stage(1) == "at_risk"
    assert compute_forgetting_stage(3) == "fading"
    assert compute_forgetting_stage(7) == "critical"
    assert compute_forgetting_stage(21) == "forgotten"


def test_get_review_priority_boosts_weak_exam_topics() -> None:
    critical = get_review_priority(
        {"topic": "quadratic equations", "stage": "critical", "severity": "severe", "confidence": 0.2},
        days_to_exam=5,
    )
    fresh = get_review_priority(
        {"topic": "linear equations", "stage": "fresh", "severity": "mild", "confidence": 0.9},
        days_to_exam=90,
    )
    assert critical > fresh
    assert 0.0 <= critical <= 10.0


def test_days_since_accepts_iso_dates() -> None:
    today = date(2026, 5, 17)
    assert days_since((today - timedelta(days=4)).isoformat(), today=today) == 4
