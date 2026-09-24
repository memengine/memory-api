from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

from api.schemas.memory_schemas import ExtractedMemory
from benchmarks.internal import phase3a_confirmation

DATASET = Path(
    "benchmarks/internal/datasets/phase3a_confirmation/development/"
    "realistic_context_v1.json"
)


def test_realistic_context_dataset_has_balanced_frozen_slices() -> None:
    cases, minimums = phase3a_confirmation.load_development_cases(DATASET)

    assert len(cases) == 84
    assert minimums == {"language": 28, "reference_type": 12}
    assert {case.language for case in cases} == {"en", "hi", "hinglish"}
    assert {case.reference_type for case in cases} == {
        "single_vague",
        "single_indirect",
        "explicit_ordinal",
        "ambiguous_multi",
        "rejection",
        "single_correction",
        "single_unrelated",
    }
    assert sum(case.require_nonproposal_memory for case in cases) == 12
    assert all(case.user_prelude for case in cases)


def test_realistic_context_places_neutral_user_turn_before_proposal() -> None:
    cases, _ = phase3a_confirmation.load_development_cases(DATASET)
    case = next(item for item in cases if item.reference_type == "single_indirect")

    messages, proposal_context, _expected = phase3a_confirmation._case_input(case)

    assert [message["role"] for message in messages] == ["user", "assistant", "user"]
    assert proposal_context[0]["turn_index"] == 1
    assert proposal_context[0]["turn_id"] == messages[1]["turn_id"]
    assert messages[2]["content"] == case.utterance


def test_direct_correction_is_not_scored_as_proposal_acceptance(
    monkeypatch,
    tmp_path,
) -> None:
    dataset = tmp_path / "correction.json"
    dataset.write_text(
        json.dumps(
            {
                "split": "development",
                "user_prelude": "Offer one possible preference without assuming it is mine.",
                "minimum_cases_per_language": 1,
                "minimum_cases_per_reference_type": 1,
                "reference_types": {
                    "single_correction": {
                        "expected_outcome": "rejected",
                        "require_nonproposal_memory": True,
                        "utterances": [
                            [
                                "en",
                                "No, that is wrong; I prefer code examples before explanations.",
                            ]
                        ],
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    class FakeExtractionService:
        def __init__(self, **_kwargs) -> None:
            pass

        async def extract(self, **_kwargs):
            return SimpleNamespace(
                memories_to_store=[
                    ExtractedMemory(
                        content="User prefers code examples before explanations.",
                        category="preference",
                        importance_score=7.0,
                        confidence=0.95,
                        expiry="permanent",
                        reasoning="Direct correction",
                        validated_evidence={"relation": "direct_user_statement"},
                    )
                ],
                pending_candidates=[],
                extraction_metadata={"candidate_validation": {"rejection_counts": {}}},
            )

    monkeypatch.setattr(phase3a_confirmation, "ExtractionService", FakeExtractionService)
    monkeypatch.setattr(phase3a_confirmation, "LLMService", lambda: object())

    record = asyncio.run(
        phase3a_confirmation.run_confirmation_evaluation(
            dataset,
            expected_split="development",
            provider_attempts=1,
        )
    )
    case = record["cases"][0]

    assert case["observed_outcome"] == "rejected"
    assert case["correct"] is True
    assert case["candidate_counts"]["proposal_stored"] == 0
    assert case["candidate_counts"]["nonproposal_stored"] == 1


def test_recovery_provider_failure_never_scores_stale_partial_result(
    monkeypatch,
    tmp_path,
) -> None:
    dataset = tmp_path / "provider-failure.json"
    dataset.write_text(
        json.dumps(
            {
                "split": "development",
                "minimum_cases_per_language": 1,
                "minimum_cases_per_reference_type": 1,
                "reference_types": {
                    "single_correction": {
                        "expected_outcome": "rejected",
                        "require_nonproposal_memory": True,
                        "utterances": [
                            [
                                "en",
                                "Reject that suggestion. I prefer conclusions first.",
                            ]
                        ],
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    class FakeExtractionService:
        def __init__(self, **_kwargs) -> None:
            pass

        async def extract(self, **_kwargs):
            return SimpleNamespace(
                memories_to_store=[],
                pending_candidates=[],
                extraction_metadata={
                    "candidate_validation": {"rejection_counts": {}},
                    "correction_recovery": {
                        "attempted": True,
                        "error": "AllProvidersFailedError",
                    },
                },
            )

    monkeypatch.setattr(phase3a_confirmation, "ExtractionService", FakeExtractionService)
    monkeypatch.setattr(phase3a_confirmation, "LLMService", lambda: object())

    record = asyncio.run(
        phase3a_confirmation.run_confirmation_evaluation(
            dataset,
            expected_split="development",
            provider_attempts=2,
        )
    )
    case = record["cases"][0]

    assert case["status"] == "error"
    assert case["attempts"] == 2
    assert case["error"]["kind"] == "provider_error"
    assert record["summary"]["error_count"] == 1
