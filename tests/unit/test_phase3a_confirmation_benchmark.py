from benchmarks.internal.phase3a_confirmation import (
    RELEASE_MINIMUMS,
    _summarize,
    load_development_cases,
)


def test_phase3a_development_calibration_has_frozen_slice_minimums() -> None:
    cases, minimums = load_development_cases()

    assert len(cases) == 100
    assert minimums == {"language": 30, "reference_type": 20}
    assert {case.language for case in cases} == {"en", "hi", "hinglish"}
    assert {case.reference_type for case in cases} == {
        "single_vague",
        "single_indirect",
        "explicit_ordinal",
        "ambiguous_multi",
        "rejection",
    }
    assert sum(case.expected_outcome == "accepted" for case in cases) == 60
    assert sum(case.expected_outcome == "pending" for case in cases) == 20
    assert sum(case.expected_outcome == "rejected" for case in cases) == 20


def test_phase3a_release_gate_requires_precision_recall_and_evidence() -> None:
    details = []
    for language in ("en", "hi", "hinglish"):
        for reference_type, expected in (
            ("single_vague", "accepted"),
            ("single_indirect", "accepted"),
            ("explicit_ordinal", "accepted"),
            ("ambiguous_multi", "pending"),
            ("rejection", "rejected"),
        ):
            for index in range(30):
                details.append(
                    {
                        "id": f"{language}-{reference_type}-{index}",
                        "language": language,
                        "reference_type": reference_type,
                        "expected_outcome": expected,
                        "observed_outcome": expected,
                        "correct": True,
                        "evidence_integrity": True,
                        "status": "completed",
                        "latency_ms": 10.0,
                        "estimated_cost_usd": 0.001,
                    }
                )

    passing = _summarize(details, {"language": 30, "reference_type": 20})
    assert passing["release_eligible"] is True
    assert passing["minimums"] == RELEASE_MINIMUMS

    details[0]["correct"] = False
    details[0]["evidence_integrity"] = False
    failing = _summarize(details, {"language": 30, "reference_type": 20})
    assert failing["release_eligible"] is False
    assert any(
        "evidence_integrity" in reason
        for reason in failing["release_gate_failures"]
    )
