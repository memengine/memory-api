"""Diagnostics must describe existing rejections, never change their outcome."""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from api.services.extraction_service import ExtractionService
from api.services.llm_service import LLMResponse


CLAIM = "For the Release Check project only, my default example language is C++."


def _memory(**overrides):
    return {
        "content": CLAIM, "category": "preference", "importance_score": 7,
        "confidence": 0.9, "claim_state": "asserted", "reasoning": "Synthetic test.",
        "evidence_turns": [0], "evidence_relation": "direct_user_statement",
        "evidence_spans": [{"turn_index": 0, "quote": CLAIM}], "proposal_turn": None,
        **overrides,
    }


def _parse(memory, *, messages=None, visible=None, counts=None):
    service = ExtractionService(llm_service=SimpleNamespace())
    return service._parse_and_validate_response(
        json.dumps({"memories": [memory], "nothing_to_extract": False}),
        messages=messages or [{"role": "user", "content": CLAIM}],
        visible_turn_indexes=visible,
        evidence_context={"extractor_version": "source-evidence-v2"},
        evidence_rejection_counts=counts,
    )


@pytest.mark.parametrize("overrides,reason", [
    ({"evidence_turns": []}, "missing_evidence"),
    ({"evidence_turns": [1]}, "invalid_evidence_turn"),
    ({"evidence_turns": [True]}, "invalid_evidence_turn"),
    ({"evidence_turns": [0, 0]}, "invalid_evidence_turn"),
    ({"evidence_relation": "forged-authority-SECRET"}, "unsupported_relation"),
    ({"evidence_spans": None}, "invalid_source_spans"),
    ({"evidence_spans": []}, "invalid_source_spans"),
    ({"evidence_spans": [{"turn_index": 0, "quote": CLAIM}] * 9}, "invalid_source_spans"),
    ({"evidence_spans": [{"turn_index": 0, "quote": CLAIM, "authority": 100}]}, "invalid_source_span_shape"),
    ({"evidence_spans": [{"turn_index": True, "quote": CLAIM}]}, "invalid_source_span_turn"),
    ({"evidence_spans": [{"turn_index": 1, "quote": CLAIM}]}, "invalid_source_span_turn"),
    ({"evidence_spans": [{"turn_index": 0, "quote": " "}]}, "invalid_source_quote"),
    ({"evidence_spans": [{"turn_index": 0, "quote": 7}]}, "invalid_source_quote"),
    ({"evidence_spans": [{"turn_index": 0, "quote": "x" * 1001}]}, "invalid_source_quote"),
    ({"evidence_spans": [{"turn_index": 0, "quote": "forged quote SECRET"}]}, "quote_not_found"),
    ({"evidence_spans": [{"turn_index": 0, "quote": CLAIM}] * 2}, "duplicate_source_span"),
    ({"content": "User prefers C++ for this project."}, "candidate_source_mismatch"),
])
def test_specific_reasons_subdivide_generic_bucket_without_changing_result(overrides, reason):
    memory = _memory(**overrides)
    counts = {}
    result = _parse(memory, counts=counts)
    assert result == _parse(memory)
    assert result == ([], [], 1, False, {"evidence_validation": 1})
    assert counts == {reason: 1}
    # Only backend-defined codes/counts, never input text or model-provided labels.
    assert "SECRET" not in json.dumps(counts)
    assert CLAIM not in json.dumps(counts)


def test_missing_spans_is_distinct_from_malformed_spans():
    memory = _memory()
    memory.pop("evidence_spans")
    counts = {}
    assert _parse(memory, counts=counts)[2] == 1
    assert counts == {"missing_source_spans": 1}


@pytest.mark.parametrize("message,visible,reason", [
    ({"role": "user", "content": CLAIM}, set(), "evidence_not_visible_to_model"),
    ({"role": "tool", "content": CLAIM}, None, "no_user_evidence"),
    ({"role": "user", "source_kind": "fetched_document", "content": CLAIM}, None, "no_user_evidence"),
    ({"role": "user", "content": CLAIM + " " + CLAIM}, None, "quote_not_unique"),
])
def test_visibility_role_and_ambiguous_quote_rejections(message, visible, reason):
    counts = {}
    assert _parse(_memory(), messages=[message], visible=visible, counts=counts)[2] == 1
    assert counts == {reason: 1}


@pytest.mark.parametrize("content", [CLAIM, "my default example language is C++."])
@pytest.mark.parametrize("state", ["asserted", "correction", "uncertain_change"])
def test_accepted_and_pending_claims_emit_no_rejection_diagnostic(content, state):
    counts = {}
    result = _parse(_memory(content=content, claim_state=state), counts=counts)
    assert result == _parse(_memory(content=content, claim_state=state))
    kept, pending, rejected, _nothing, coarse = result
    assert rejected == 0 and counts == coarse == {}
    candidate = (kept + pending)[0]
    assert candidate.content == CLAIM
    assert candidate.validated_evidence["authority"] == {"level": 20, "label": "client_assertion"}


def test_non_evidence_rejection_is_not_mislabelled():
    counts = {}
    assert _parse(_memory(category="invalid"), counts=counts)[4] == {"invalid_category": 1}
    assert counts == {}


def test_question_only_source_retains_existing_rejection():
    question = "Which language should you use for Release Check examples?"
    counts = {}
    assert _parse(_memory(content=question, evidence_spans=[{"turn_index": 0, "quote": question}]),
                  messages=[{"role": "user", "content": question}], counts=counts)[2] == 1
    assert counts == {"unsupported_user_evidence": 1}


def test_counts_aggregate_one_reason_per_generic_rejection():
    service = ExtractionService(llm_service=SimpleNamespace())
    counts = {}
    payload = json.dumps({"memories": [
        _memory(content="Unattributable generated preference."),
        _memory(content="Another generated preference."),
        _memory(evidence_turns=[99]),
        _memory(),
    ]})
    result = service._parse_and_validate_response(
        payload, messages=[{"role": "user", "content": CLAIM}],
        evidence_context={"extractor_version": "source-evidence-v2"},
        evidence_rejection_counts=counts,
    )
    assert len(result[0]) == 1 and result[2] == 3
    assert result[4] == {"evidence_validation": 3}
    assert counts == {"candidate_source_mismatch": 2, "invalid_evidence_turn": 1}
    assert sum(counts.values()) == result[4]["evidence_validation"]


def test_successful_direct_fallback_discards_proposal_failure():
    claim = "No, that is wrong. My example language is Python."
    messages = [
        {"role": "assistant", "content": "My example language is C++.",
         "source_kind": "assistant_output", "is_memory_proposal": True,
         "turn_id": "proposal", "turn_content_sha256": "hash"},
        {"role": "user", "content": claim},
    ]
    service = ExtractionService(llm_service=SimpleNamespace(), proposal_confirmation_enabled=True)
    counts = {}
    result = service._parse_and_validate_response(
        json.dumps({"memories": [_memory(
            content=claim, evidence_turns=[1], proposal_turn=0,
            evidence_relation="user_confirmed_assistant_proposal",
            evidence_spans=[{"turn_index": 1, "quote": claim}],
        )]}), messages=messages, evidence_context={"extractor_version": "source-evidence-v2"},
        proposal_context=[{"id": "p", "group_id": "g", "ordinal": 1,
                           "turn_index": 0, "turn_id": "proposal", "content_sha256": "hash"}],
        evidence_rejection_counts=counts,
    )
    assert len(result[0]) == 1 and result[2] == 0 and result[4] == counts == {}
    assert result[0][0].validated_evidence["relation"] == "direct_user_statement"


@pytest.mark.asyncio
async def test_extract_metadata_contains_only_bounded_diagnostics_without_extra_call(caplog):
    caplog.set_level("INFO", logger="api.services.extraction_service")
    memory = _memory(content="Generated claim SECRET not attributable to the source.")
    complete = AsyncMock(return_value=LLMResponse(
        json.dumps({"memories": [memory], "nothing_to_extract": False}),
        "test", "fake", 11, 7, 18, 1,
    ))
    service = ExtractionService(llm_service=SimpleNamespace(complete=complete))
    result = await service.extract(messages=[{"role": "user", "content": CLAIM}])
    assert complete.await_count == 1
    assert result.memories_to_store == result.pending_candidates == []
    metadata = result.extraction_metadata["candidate_validation"]
    assert metadata["rejection_counts"] == {"evidence_validation": 1}
    assert metadata["evidence_rejection_counts"] == {"candidate_source_mismatch": 1}
    assert CLAIM not in json.dumps(metadata) and "SECRET" not in json.dumps(metadata)
    assert "SECRET" not in caplog.text
    for record in caplog.records:
        assert "SECRET" not in str(record.__dict__) and CLAIM not in str(record.__dict__)


@pytest.mark.asyncio
@pytest.mark.parametrize("pass_name", ["empty_response_repair", "correction_recovery"])
async def test_existing_additional_passes_aggregate_diagnostics(monkeypatch, pass_name):
    rejected = _memory(evidence_spans=[{"turn_index": 0, "quote": "Not present in the source."}])
    primary = {"memories": [], "nothing_to_extract": False} if pass_name == "empty_response_repair" else {"memories": [rejected]}
    responses = [primary, {"memories": [_memory(), rejected], "nothing_to_extract": False}]
    complete = AsyncMock(side_effect=[
        LLMResponse(json.dumps(payload), "test", "fake", 11, 7, 18, 1)
        for payload in responses
    ])
    service = ExtractionService(llm_service=SimpleNamespace(complete=complete))
    # Exercise wiring of already existing passes, not a new retry policy.
    monkeypatch.setattr(service, "_should_attempt_correction_recovery", lambda **kwargs: pass_name == "correction_recovery")
    result = await service.extract(messages=[{"role": "user", "content": CLAIM}])
    expected = 1 if pass_name == "empty_response_repair" else 2
    assert complete.await_count == 2
    assert len(result.memories_to_store) == 1 and not result.pending_candidates
    assert result.extraction_metadata[pass_name]["attempted"] is True
    metadata = result.extraction_metadata["candidate_validation"]
    assert metadata["rejection_counts"] == {"evidence_validation": expected}
    assert metadata["evidence_rejection_counts"] == {"quote_not_found": expected}
