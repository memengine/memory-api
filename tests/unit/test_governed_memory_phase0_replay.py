from __future__ import annotations

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "governed_memory_phase0_replay.py"
SPEC = importlib.util.spec_from_file_location("governed_memory_phase0_replay", SCRIPT)
assert SPEC and SPEC.loader
replay = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(replay)


def test_phase0_fixture_uses_only_public_memory_contract_inputs() -> None:
    fixture = replay.load_fixture(replay.DEFAULT_FIXTURE)

    assert fixture["version"] == "phase0-v1"
    assert {item["id"] for item in fixture["scenarios"]} == {
        "uncertain_programming_language_change",
        "exam_date_correction",
    }
    for scenario in fixture["scenarios"]:
        payload = replay.build_add_payload(
            scenario,
            external_user_id="phase0-test-user",
            conversation_id="phase0:test-conversation",
            phase="initial",
        )
        assert set(payload) == {
            "external_user_id",
            "messages",
            "conversation_id",
            "evidence_mode",
            "metadata",
        }
        assert payload["evidence_mode"] == "conversation_evidence"


def test_phase0_evaluator_requires_clarification_for_uncertain_change() -> None:
    expected = {
        "clarification_required": True,
        "must_include_any": ["c++"],
        "must_exclude_all": ["python"],
    }

    passing = replay.evaluate_retrieval(
        {
            "clarification": {"id": "clarification-1"},
            "data": [{"content": "The user's current programming default is C++."}],
        },
        expected,
    )
    failing = replay.evaluate_retrieval(
        {
            "clarification": None,
            "data": [
                {"content": "The user's programming default is C++."},
                {"content": "The user's programming default is Python."},
            ],
        },
        expected,
    )

    assert passing["passed"] is True
    assert failing["passed"] is False
    assert failing["checks"] == {
        "required_value": True,
        "superseded_value_absent": False,
        "clarification": False,
    }


def test_phase0_evaluator_requires_only_corrected_exam_date() -> None:
    result = replay.evaluate_retrieval(
        {
            "clarification": None,
            "data": [
                {"content": "The user's exam is on October 10."},
                {"content": "The user's exam is on October 18."},
            ],
        },
        {
            "clarification_required": False,
            "must_include_any": ["october 18"],
            "must_exclude_all": ["october 10"],
        },
    )

    assert result["passed"] is False
    assert result["checks"]["superseded_value_absent"] is False


def test_phase0_scenario_accepts_one_successful_clarification_delivery() -> None:
    expected = {
        "clarification_required": True,
        "must_include_any": ["c++"],
        "must_exclude_all": ["python"],
    }
    immediate = {
        "clarification": {"id": "clarification-1"},
        "data": [{"content": "The user's programming default is C++."}],
    }
    settled = {
        "clarification": None,
        "data": [{"content": "The user's programming default is C++."}],
    }

    result = replay.evaluate_scenario(immediate, settled, expected)

    assert result["passed"] is True
    assert result["clarification_observations"] == [True, False]
