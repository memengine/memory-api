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

    assert fixture["version"] == "phase0-v2"
    assert {item["id"] for item in fixture["scenarios"]} == {
        "uncertain_programming_language_change",
        "project_name_correction",
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


def test_phase0_evaluator_rejects_superseded_active_memory_id() -> None:
    result = replay.evaluate_retrieval(
        {
            "clarification": None,
            "data": [
                {"id": "old-id", "content": "The user's project is Atlas."},
                {
                    "id": "new-id",
                    "content": "The user's project was renamed from Atlas to Nova.",
                },
            ],
        },
        {
            "clarification_required": False,
            "must_include_any": ["nova"],
            "must_exclude_all": [],
        },
        excluded_memory_ids={"old-id"},
    )

    assert result["passed"] is False
    assert result["checks"]["superseded_memory_absent"] is False


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


def test_phase0_correction_allows_safe_empty_immediate_but_requires_settled_value() -> None:
    expected = {
        "clarification_required": False,
        "require_initial_memory": True,
        "exclude_initial_memory": True,
        "allow_empty_immediate": True,
        "must_include_any": ["nova"],
        "must_exclude_all": [],
    }
    result = replay.evaluate_scenario(
        {"clarification": None, "data": []},
        {
            "clarification": None,
            "data": [
                {
                    "id": "new-id",
                    "content": "The project was renamed from Atlas to Nova.",
                }
            ],
        },
        expected,
        initial_memory_ids={"old-id"},
    )

    assert result["passed"] is True
    assert result["checks"]["immediate_memory_state"] is True
    assert result["checks"]["settled_memory_state"] is True
    assert result["checks"]["initial_memory_created"] is True


def test_phase0_correction_fails_when_initial_memory_was_not_created() -> None:
    expected = {
        "clarification_required": False,
        "require_initial_memory": True,
        "exclude_initial_memory": True,
        "allow_empty_immediate": True,
        "must_include_any": ["nova"],
        "must_exclude_all": [],
    }
    result = replay.evaluate_scenario(
        {"clarification": None, "data": []},
        {
            "clarification": None,
            "data": [{"id": "new-id", "content": "The project is Nova."}],
        },
        expected,
        initial_memory_ids=set(),
    )

    assert result["passed"] is False
    assert result["checks"]["initial_memory_created"] is False
