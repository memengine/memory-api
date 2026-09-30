from __future__ import annotations

import importlib.util
from collections import Counter
from pathlib import Path

import pytest

from api.schemas.requests import MemoryAddRequest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "governed_memory_phase0_replay.py"
DATASET = (
    ROOT
    / "benchmarks"
    / "internal"
    / "datasets"
    / "governed_memory"
    / "development"
    / "journeys_v1.json"
)
SPEC = importlib.util.spec_from_file_location("governed_memory_journey_runner", SCRIPT)
assert SPEC and SPEC.loader
runner = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(runner)


def test_development_journey_dataset_has_frozen_inventory() -> None:
    fixture = runner.load_fixture(DATASET)
    scenarios = fixture["scenarios"]

    assert fixture["split"] == "development"
    assert fixture["version"] == "governed-memory-development-v1"
    assert len(scenarios) == 42
    assert Counter(item["language"] for item in scenarios) == {
        "en": 10,
        "hi": 10,
        "hinglish": 10,
        "system": 12,
    }
    assert sum(item["safety_critical"] for item in scenarios) == 39
    assert len({item["id"] for item in scenarios}) == len(scenarios)


def test_development_journeys_cover_governance_boundaries() -> None:
    fixture = runner.load_fixture(DATASET)
    scenarios = fixture["scenarios"]
    families = {item["family"] for item in scenarios}

    assert {
        "authority_boundary",
        "both_resolution",
        "direct_correction",
        "evidence_integrity",
        "identity_isolation",
        "idempotency",
        "indirect_uncertainty",
        "late_reference",
        "neither_resolution",
        "noisy_uncertainty",
        "proposal_ambiguity",
        "proposal_rejection",
        "proposal_selection",
        "quoted_non_commitment",
        "uncertain_conflict",
    }.issubset(families)
    source_kinds = {
        message.get("source_kind")
        for scenario in scenarios
        for field in ("initial_messages", "update_messages")
        for message in scenario[field]
    }
    assert {"assistant_output", "fetched_document", "tool_output"}.issubset(
        source_kinds
    )


def test_every_development_journey_uses_the_real_public_add_contract() -> None:
    fixture = runner.load_fixture(DATASET)

    for scenario in fixture["scenarios"]:
        for phase in ("initial", "update"):
            payload = runner.build_add_payload(
                scenario,
                external_user_id="development-contract-user",
                conversation_id="development-contract-conversation",
                phase=phase,
                fixture_version=fixture["version"],
            )
            MemoryAddRequest.model_validate(payload)


def test_retrieval_evaluator_can_require_every_current_value() -> None:
    passing = runner.evaluate_retrieval(
        {
            "clarification": None,
            "data": [
                {"content": "Email is current."},
                {"content": "Slack is also current."},
            ],
        },
        {
            "clarification_required": False,
            "must_include_all": ["email", "slack"],
            "must_exclude_all": [],
        },
    )
    failing = runner.evaluate_retrieval(
        {"clarification": None, "data": [{"content": "Email is current."}]},
        {
            "clarification_required": False,
            "must_include_all": ["email", "slack"],
            "must_exclude_all": [],
        },
    )

    assert passing["passed"] is True
    assert failing["checks"]["all_required_values"] is False


def test_clarification_selector_supports_both_and_neither() -> None:
    clarification = {
        "options": [
            {"answer": "A", "label": "Email", "memory_id": "email-id"},
            {"answer": "B", "label": "Chat", "memory_id": "chat-id"},
            {"answer": "both", "label": "Both are still correct"},
            {"answer": "neither", "label": "Neither is correct"},
        ]
    }

    assert runner.select_clarification_answer(clarification, "both")["answer"] == "both"
    assert (
        runner.select_clarification_answer(clarification, "neither")["answer"]
        == "neither"
    )


def test_provenance_evaluator_checks_exact_authority_tier() -> None:
    response = {
        "data": [
            {
                "id": "memory-1",
                "provenance": {
                    "event_id": "event-1",
                    "external_conversation_id": "conversation-1",
                    "extraction_evidence": {
                        "authority": {"label": "client_assertion", "level": 20},
                        "turn_references": [{"role": "user", "turn_id": "turn-1"}],
                    },
                },
            }
        ]
    }

    passing = runner.evaluate_provenance(
        response,
        expected_conversation_id="conversation-1",
        expected_authority_label="client_assertion",
        expected_authority_level=20,
    )
    failing = runner.evaluate_provenance(
        response,
        expected_conversation_id="conversation-1",
        expected_authority_label="direct_user_input",
        expected_authority_level=60,
    )

    assert passing["passed"] is True
    assert failing["passed"] is False


def test_empty_post_neither_state_is_valid_only_when_explicitly_allowed() -> None:
    assert (
        runner.evaluate_provenance(
            {"data": []},
            expected_conversation_id="conversation-1",
            allow_empty=True,
        )["passed"]
        is True
    )
    assert (
        runner.evaluate_provenance(
            {"data": []},
            expected_conversation_id="conversation-1",
        )["passed"]
        is False
    )


def test_timing_summary_keeps_answer_path_separate_from_async_processing() -> None:
    summary = runner._timing_summary(
        [
            {
                "initial_add": {
                    "acknowledgement_ms": 100.0,
                    "terminal": {"latency_ms": 1500.0},
                },
                "update_add": {
                    "acknowledgement_ms": 200.0,
                    "terminal": {"latency_ms": 2500.0},
                },
                "warm_retrieval": {"latency_ms": 300.0},
                "immediate_retrieval": {"latency_ms": 400.0},
                "settled_retrieval": {"latency_ms": 500.0},
                "resolution": {"latency_ms": 600.0},
            }
        ]
    )

    assert summary["add_acknowledgement"] == {
        "count": 2,
        "p50": 150.0,
        "p95": 200.0,
        "max": 200.0,
    }
    assert summary["job_polling"]["p50"] == 2000.0
    assert summary["retrieval"]["count"] == 3
    assert summary["clarification_answer"]["p50"] == 600.0


def test_scenario_selection_is_explicit_bounded_and_ordered() -> None:
    fixture = runner.load_fixture(DATASET)
    selected = runner.select_scenarios(
        fixture,
        scenario_ids=[
            "system_tool_output_override",
            "en_direct_project_correction",
        ],
        max_cases=1,
    )

    assert [item["id"] for item in selected] == ["system_tool_output_override"]
    with pytest.raises(ValueError, match="Unknown scenario IDs"):
        runner.select_scenarios(
            fixture,
            scenario_ids=["not-a-real-scenario"],
            max_cases=None,
        )


def test_fixture_rejects_resolution_without_post_state() -> None:
    fixture = {
        "version": "invalid",
        "scenarios": [
            {
            "id": "invalid-resolution",
                "initial_messages": [{"role": "user", "content": "A"}],
                "update_messages": [{"role": "user", "content": "B"}],
                "warm_query": "A?",
                "verification_query": "B?",
                "expected": {
                    "clarification_required": True,
                    "resolve_with_answer": "both",
                },
            }
        ],
    }

    with pytest.raises(ValueError, match="resolution selector and after_resolution"):
        runner.validate_fixture(fixture)
