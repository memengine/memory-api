import json

import pytest

from benchmarks.internal.cases import HOLDOUT_APPROVAL_ENV, HOLDOUT_APPROVAL_TOKEN
from benchmarks.internal.phase3a_confirmation import (
    HOLDOUT_REFERENCE_OUTCOMES,
    RELEASE_MINIMUMS,
    _case_input,
    _summarize,
    load_confirmation_cases,
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


def _write_holdout(tmp_path, entry=None, *, schema_version="2.0"):
    languages = ["en"] * 15 + ["hi"] * 15 + ["hinglish"] * 20
    language_index = 0
    groups = {}
    for reference_type, expected_outcome in HOLDOUT_REFERENCE_OUTCOMES.items():
        utterances = []
        for case_index in range(10):
            proposal_count = (
                2
                if reference_type in {"explicit_ordinal", "ambiguous_multi"}
                else 1
            )
            proposals = [
                {
                    "content": (
                        f"Independent proposal {reference_type} {case_index} {ordinal}."
                    ),
                    "memory": (
                        f"Independent memory {reference_type} {case_index} {ordinal}."
                    ),
                    "category": "preference",
                }
                for ordinal in range(1, proposal_count + 1)
            ]
            utterances.append(
                {
                    "language": languages[language_index],
                    "utterance": f"Independent response {language_index}.",
                    "target_ordinal": 1 if expected_outcome == "accepted" else None,
                    "proposals": proposals,
                }
            )
            language_index += 1
        groups[reference_type] = {
            "expected_outcome": expected_outcome,
            "utterances": utterances,
        }
    if entry is not None:
        groups["single_vague"]["utterances"][0] = entry
    dataset = tmp_path / "holdout.json"
    dataset.write_text(
        json.dumps(
            {
                "schema_version": schema_version,
                "split": "holdout",
                "minimum_cases_per_language": 15,
                "minimum_cases_per_reference_type": 10,
                "reference_types": groups,
            }
        ),
        encoding="utf-8",
    )
    return dataset


def test_phase3a_holdout_uses_case_specific_proposals(monkeypatch, tmp_path) -> None:
    dataset = _write_holdout(
        tmp_path,
        {
            "language": "en",
            "utterance": "Please keep that in mind.",
            "target_ordinal": 1,
            "proposals": [
                {
                    "content": "I can remember that you avoid meetings before noon.",
                    "memory": "User avoids meetings before noon.",
                    "category": "preference",
                }
            ],
        },
    )
    monkeypatch.setenv(HOLDOUT_APPROVAL_ENV, HOLDOUT_APPROVAL_TOKEN)

    cases, _ = load_confirmation_cases(dataset, expected_split="holdout")
    messages, _, expected = _case_input(cases[0])

    assert messages[0]["content"] == (
        "I can remember that you avoid meetings before noon.\n"
        "Proposed memory: User avoids meetings before noon."
    )
    assert messages[0]["proposed_memory"] == {
        "content": "User avoids meetings before noon.",
        "category": "preference",
    }
    assert expected[1] == {
        "content": "I can remember that you avoid meetings before noon.",
        "memory": "User avoids meetings before noon.",
        "category": "preference",
    }


def test_phase3a_holdout_rejects_legacy_shared_proposals(monkeypatch, tmp_path) -> None:
    dataset = _write_holdout(
        tmp_path,
        ["en", "Yes, remember that."],
        schema_version="1.0",
    )
    monkeypatch.setenv(HOLDOUT_APPROVAL_ENV, HOLDOUT_APPROVAL_TOKEN)

    with pytest.raises(ValueError, match="schema_version 2.0"):
        load_confirmation_cases(dataset, expected_split="holdout")


def test_phase3a_holdout_rejects_target_outside_proposals(monkeypatch, tmp_path) -> None:
    dataset = _write_holdout(
        tmp_path,
        {
            "language": "en",
            "utterance": "Remember the second one.",
            "target_ordinal": 2,
            "proposals": [
                {
                    "content": "I can remember that you avoid meetings before noon.",
                    "memory": "User avoids meetings before noon.",
                    "category": "preference",
                }
            ],
        },
    )
    monkeypatch.setenv(HOLDOUT_APPROVAL_ENV, HOLDOUT_APPROVAL_TOKEN)

    with pytest.raises(ValueError, match="Invalid target_ordinal"):
        load_confirmation_cases(dataset, expected_split="holdout")


def test_phase3a_holdout_cannot_lower_frozen_slice_minimums(
    monkeypatch, tmp_path
) -> None:
    dataset = _write_holdout(tmp_path)
    payload = json.loads(dataset.read_text(encoding="utf-8"))
    payload["minimum_cases_per_language"] = 1
    dataset.write_text(json.dumps(payload), encoding="utf-8")
    monkeypatch.setenv(HOLDOUT_APPROVAL_ENV, HOLDOUT_APPROVAL_TOKEN)

    with pytest.raises(ValueError, match="frozen slice minimums"):
        load_confirmation_cases(dataset, expected_split="holdout")


def test_phase3a_holdout_rejects_visible_development_proposal(
    monkeypatch, tmp_path
) -> None:
    dataset = _write_holdout(tmp_path)
    payload = json.loads(dataset.read_text(encoding="utf-8"))
    payload["reference_types"]["single_vague"]["utterances"][0]["proposals"] = [
        {
            "content": (
                "I can remember that you prefer one-line diagnoses before "
                "numbered troubleshooting steps."
            ),
            "memory": (
                "User prefers one-line diagnoses before numbered troubleshooting steps."
            ),
            "category": "preference",
        }
    ]
    dataset.write_text(json.dumps(payload), encoding="utf-8")
    monkeypatch.setenv(HOLDOUT_APPROVAL_ENV, HOLDOUT_APPROVAL_TOKEN)

    with pytest.raises(ValueError, match="visible development proposal"):
        load_confirmation_cases(dataset, expected_split="holdout")
