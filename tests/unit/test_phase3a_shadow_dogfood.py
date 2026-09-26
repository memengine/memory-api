from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

from api.schemas.requests import MemoryAddRequest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "phase3a_shadow_dogfood.py"
SPEC = importlib.util.spec_from_file_location("phase3a_shadow_dogfood", SCRIPT)
assert SPEC and SPEC.loader
dogfood = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = dogfood
SPEC.loader.exec_module(dogfood)


def test_fixture_has_one_case_per_required_slice() -> None:
    cases = dogfood.load_fixture(dogfood.DEFAULT_FIXTURE)["cases"]

    assert len(cases) == 15
    assert {
        (case["language"], case["reference_type"])
        for case in cases
    } == {
        (language, reference_type)
        for language in dogfood.EXPECTED_LANGUAGES
        for reference_type in dogfood.EXPECTED_REFERENCE_TYPES
    }
    assert all(case["expected_outcome"] in {"accepted", "pending", "rejected"} for case in cases)


def test_payload_uses_structured_proposal_contract_and_isolates_each_case() -> None:
    case = dogfood.load_fixture(dogfood.DEFAULT_FIXTURE)["cases"][0]
    payload = dogfood.build_payload(case, "test-run")

    assert set(payload) == {
        "external_user_id",
        "conversation_id",
        "messages",
        "metadata",
        "evidence_mode",
    }
    assert payload["external_user_id"].startswith("phase3a-shadow-test-run-")
    assert payload["conversation_id"].startswith("phase3a-shadow:test-run:")
    assert payload["evidence_mode"] == "conversation_evidence"
    proposal = payload["messages"][0]
    assert proposal["role"] == "assistant"
    assert proposal["source_kind"] == "assistant_output"
    assert proposal["is_memory_proposal"] is True
    assert proposal["proposed_memory"]["content"] in proposal["content"]
    assert payload["messages"][-1]["source_kind"] == "direct_user_input"


def test_every_fixture_case_is_accepted_by_the_public_request_schema() -> None:
    cases = dogfood.load_fixture(dogfood.DEFAULT_FIXTURE)["cases"]

    for case in cases:
        request = MemoryAddRequest.model_validate(
            dogfood.build_payload(case, "schema-check")
        )
        assert request.messages[-1].role == "user"
        assert all(
            message.proposed_memory is not None
            for message in request.messages[:-1]
        )


def test_bounded_observation_drops_content_and_unrelated_metadata() -> None:
    job = {
        "extraction_metadata": {
            "raw_prompt": "must not escape",
            "phase3a_confirmation_shadow": {
                "status": "completed",
                "outcome": "accepted",
                "latency_ms": 12,
                "candidate_content": "must not escape either",
                "messages": [{"content": "private"}],
            },
        }
    }

    observation = dogfood.bounded_observation(job)

    assert observation == {
        "status": "completed",
        "outcome": "accepted",
        "latency_ms": 12,
    }
    assert "must not escape" not in str(observation)
    assert "private" not in str(observation)
