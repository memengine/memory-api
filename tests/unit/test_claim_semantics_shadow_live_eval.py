from __future__ import annotations

import json

import pytest

from scripts.claim_semantics_shadow_live_eval import (
    TERMINAL_JOB_STATUSES,
    evaluate_shadow,
    load_cases,
    summarize,
)

EXPECTED = {
    "predicate": "programming.default_language",
    "speech_act": "correction",
    "certainty": "certain",
    "temporal_kind": "permanent",
}


def test_retryable_failed_job_is_not_terminal() -> None:
    assert "failed" not in TERMINAL_JOB_STATUSES
    assert "dead" in TERMINAL_JOB_STATUSES


def test_evaluate_shadow_requires_one_matching_observation() -> None:
    metadata = {
        "claim_semantics_shadow": {
            "observations": [{**EXPECTED, "value_sha256": "digest"}]
        }
    }

    result = evaluate_shadow(metadata, EXPECTED)

    assert result["passed"] is True
    assert all(result["field_matches"].values())


def test_evaluate_shadow_rejects_missing_or_multiple_observations() -> None:
    assert evaluate_shadow({}, EXPECTED)["reason"] == "missing_shadow_metadata"
    result = evaluate_shadow(
        {"claim_semantics_shadow": {"observations": [EXPECTED, EXPECTED]}}, EXPECTED
    )
    assert result["passed"] is False
    assert result["observation_count"] == 2


def test_load_cases_rejects_incomplete_expectation(tmp_path) -> None:
    path = tmp_path / "cases.json"
    path.write_text(
        json.dumps(
            {
                "cases": [
                    {
                        "id": "case-1",
                        "language": "en",
                        "utterance": "Remember this.",
                        "expected": {"predicate": "profile.note"},
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="expected"):
        load_cases(path)


def test_summarize_reports_exact_and_field_accuracy() -> None:
    records = [
        {
            "language": "en",
            "evaluation": {
                "passed": True,
                "field_matches": {field: True for field in EXPECTED},
            },
            "shadow": {"rejection_counts": {}},
            "primary_pass": {"latency_ms": 10, "input_tokens": 20, "output_tokens": 5},
            "acknowledgement_ms": 2.0,
            "processing_ms": 12.0,
        },
        {
            "language": "hi",
            "evaluation": {
                "passed": False,
                "field_matches": {
                    "predicate": True,
                    "speech_act": False,
                    "certainty": True,
                    "temporal_kind": True,
                },
            },
            "shadow": {"rejection_counts": {"invalid_evidence_quote": 1}},
            "primary_pass": {"latency_ms": 30, "input_tokens": 22, "output_tokens": 6},
            "acknowledgement_ms": 4.0,
            "processing_ms": 32.0,
        },
    ]

    result = summarize(records)

    assert result["exact_match_rate"] == 0.5
    assert result["field_accuracy"]["speech_act"] == 0.5
    assert result["tokens"]["total"] == 53
    assert result["rejection_counts"] == {"invalid_evidence_quote": 1}
