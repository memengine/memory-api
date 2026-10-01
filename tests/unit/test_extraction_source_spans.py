from __future__ import annotations

import hashlib
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from api.services.extraction_service import ExtractionService
from api.services.llm_service import LLMResponse


def _candidate(content: str) -> SimpleNamespace:
    return SimpleNamespace(content=content)


@pytest.mark.parametrize("content,source,accepted", [
    ("常に説明の前にコード例を示してください。", "プログラミングの説明では、常に説明の前にコード例を示してください。", True),
    ("I might prefer Python", "I might prefer Python, but I have not replaced C++.", True),
    ("User prefers Python", "I might prefer Python, but I have not replaced C++.", False),
])
def test_primary_parser_expands_only_exact_subclauses_to_complete_verified_quote(content, source, accepted):
    service = ExtractionService(llm_service=SimpleNamespace())
    payload = {"memories": [{
        "content": content, "category": "preference", "importance_score": 7,
        "confidence": 0.9, "claim_state": "asserted", "reasoning": "Development source test.",
        "evidence_turns": [0], "evidence_relation": "direct_user_statement",
        "evidence_spans": [{"turn_index": 0, "quote": source}], "proposal_turn": None,
    }], "nothing_to_extract": False}
    kept, pending, filtered, _nothing, _counts = service._parse_and_validate_response(
        json.dumps(payload), messages=[{"role": "user", "content": source}],
        evidence_context={"extractor_version": "source-evidence-v2"},
    )
    assert bool(kept) == accepted
    assert not pending
    if accepted:
        assert kept[0].content == source
        assert kept[0].validated_evidence["source_spans"][0]["turn_sha256"] == hashlib.sha256(source.encode()).hexdigest()
    else:
        assert filtered == 1


@pytest.mark.parametrize(
    "claim",
    [
        "I prefer code examples before explanations.",
        "मुझे व्याख्या से पहले कोड उदाहरण पसंद हैं।",
        "Mujhe explanation se pehle code examples pasand hain.",
        "説明の前にコード例を示してください。",
        "أفضل أمثلة البرمجة قبل الشرح.",
    ],
)
def test_source_quotes_preserve_script_and_derive_exact_offsets(claim: str) -> None:
    prefix = "👋 Context. "
    source = prefix + claim
    evidence = ExtractionService._validated_user_evidence(
        _candidate(claim),
        [{"role": "user", "content": source, "turn_id": "turn-1"}],
        [0],
        "direct_user_statement",
        evidence_spans=[{"turn_index": 0, "quote": claim}],
    )

    assert evidence["schema_version"] == 2
    assert evidence["grounding_mode"] == "verified_source_spans"
    assert evidence["authority"] == {"level": 20, "label": "client_assertion"}
    span = evidence["source_spans"][0]
    assert span["turn_id"] == "turn-1"
    assert span["start_char"] == len(prefix)
    assert source[span["start_char"] : span["end_char"]] == claim
    assert span["quote_sha256"] == hashlib.sha256(claim.encode("utf-8")).hexdigest()
    assert span["offset_unit"] == "unicode_code_point"
    assert "quote" not in span


@pytest.mark.parametrize(
    "spans",
    [
        None,
        [],
        "not an array",
        [{"turn_index": True, "quote": "I prefer concise answers."}],
        [{"turn_index": -1, "quote": "I prefer concise answers."}],
        [{"turn_index": 9, "quote": "I prefer concise answers."}],
        [{"turn_index": 0, "quote": "I prefer detailed answers."}],
        [{"turn_index": 0, "quote": "   "}],
        [{"turn_index": 0, "quote": 123}],
        [{"turn_index": 0, "quote": "x" * 1001}],
        [{"turn_index": 0, "quote": "I prefer concise answers.", "authority": 100}],
        [{"turn_index": 0, "quote": "I prefer concise answers."}] * 2,
        [{"turn_index": 0, "quote": "I prefer concise answers."}] * 9,
    ],
)
def test_invalid_or_forged_quotes_never_fall_back_to_whole_turn(spans) -> None:
    assert (
        ExtractionService._validated_user_evidence(
            _candidate("User prefers concise answers."),
            [{"role": "user", "content": "I prefer concise answers."}],
            [0],
            "direct_user_statement",
            evidence_spans=spans,
        )
        == {}
    )


@pytest.mark.parametrize(
    "source_kind", ["tool_output", "fetched_document", "assistant_output"]
)
def test_quote_cannot_launder_ineligible_content(source_kind: str) -> None:
    quote = "I prefer concise answers."
    messages = [
        {"role": "user", "content": "Hello there."},
        {"role": "user", "source_kind": source_kind, "content": quote},
    ]
    assert (
        ExtractionService._validated_user_evidence(
            _candidate(quote),
            messages,
            [0, 1],
            "direct_user_statement",
            evidence_spans=[{"turn_index": 1, "quote": quote}],
        )
        == {}
    )


def test_quote_must_come_from_cited_visible_turn() -> None:
    messages = [
        {"role": "user", "content": "I prefer concise answers."},
        {"role": "user", "content": "I prefer detailed answers."},
    ]
    spans = [{"turn_index": 1, "quote": messages[1]["content"]}]
    assert (
        ExtractionService._validated_user_evidence(
            _candidate(messages[1]["content"]),
            messages,
            [0],
            "direct_user_statement",
            evidence_spans=spans,
        )
        == {}
    )
    assert (
        ExtractionService._validated_user_evidence(
            _candidate(messages[1]["content"]),
            messages,
            [1],
            "direct_user_statement",
            visible_turn_indexes={0},
            evidence_spans=spans,
        )
        == {}
    )


def test_valid_unrelated_quote_cannot_use_words_outside_quoted_clause() -> None:
    assert (
        ExtractionService._validated_user_evidence(
            _candidate("User prefers detailed answers."),
            [
                {
                    "role": "user",
                    "content": "My colleague likes detailed answers. I use Neovim.",
                }
            ],
            [0],
            "direct_user_statement",
            evidence_spans=[{"turn_index": 0, "quote": "I use Neovim."}],
        )
        == {}
    )


@pytest.mark.parametrize(
    "candidate",
    [
        "User prefers code examples before explanations.",
        "मुझे व्याख्या के बाद कोड उदाहरण चाहिए।",
    ],
)
def test_v2_rejects_translated_or_changed_claim_despite_real_quote(candidate: str) -> None:
    quote = "मुझे व्याख्या से पहले कोड उदाहरण चाहिए।"
    assert ExtractionService._validated_user_evidence(
        _candidate(candidate),
        [{"role": "user", "content": quote}],
        [0],
        "direct_user_statement",
        evidence_spans=[{"turn_index": 0, "quote": quote}],
    ) == {}


def test_v2_preserves_short_non_latin_claim_without_word_tokenization() -> None:
    quote = "爱茶"
    evidence = ExtractionService._validated_user_evidence(
        _candidate(quote),
        [{"role": "user", "content": quote}],
        [0],
        "direct_user_statement",
        evidence_spans=[{"turn_index": 0, "quote": quote}],
    )
    assert evidence["grounding_mode"] == "verified_source_spans"


def test_repeated_quote_requires_unambiguous_surrounding_text() -> None:
    text = "I like Python. She said: I like Python."
    assert (
        ExtractionService._validated_user_evidence(
            _candidate("User likes Python."),
            [{"role": "user", "content": text}],
            [0],
            "direct_user_statement",
            evidence_spans=[{"turn_index": 0, "quote": "I like Python."}],
        )
        == {}
    )


def test_legacy_responses_keep_existing_gate_and_are_identifiable() -> None:
    evidence = ExtractionService._validated_user_evidence(
        _candidate("User prefers concise answers."),
        [{"role": "user", "content": "I prefer concise answers."}],
        [0],
        "direct_user_statement",
    )
    assert evidence["schema_version"] == 1
    assert evidence["grounding_mode"] == "legacy_token_overlap"
    assert "source_spans" not in evidence


def test_v2_provider_cannot_omit_spans_to_use_legacy_gate() -> None:
    claim = "I prefer concise answers."
    assert ExtractionService._validated_user_evidence(
        _candidate(claim),
        [{"role": "user", "content": claim}],
        [0],
        "direct_user_statement",
        evidence_context={"extractor_version": "source-evidence-v2"},
    ) == {}


def test_cross_turn_claim_preserves_source_clauses_in_transcript_order() -> None:
    first = "We are building an analytics platform."
    second = "Mostly for healthcare operations teams."
    messages = [
        {"role": "user", "content": first},
        {"role": "assistant", "content": "Who is it for?"},
        {"role": "user", "content": second},
    ]
    spans = [{"turn_index": 2, "quote": second}, {"turn_index": 0, "quote": first}]
    evidence = ExtractionService._validated_user_evidence(
        _candidate(first + "\n" + second), messages, [0, 2], "direct_user_statement",
        evidence_spans=spans,
    )
    assert [span["turn_index"] for span in evidence["source_spans"]] == [0, 2]
    assert ExtractionService._validated_user_evidence(
        _candidate(second + "\n" + first), messages, [0, 2], "direct_user_statement",
        evidence_spans=spans,
    ) == {}


@pytest.mark.asyncio
async def test_hindi_source_spans_survive_primary_extraction_without_extra_call() -> None:
    claim = "मुझे व्याख्या से पहले कोड उदाहरण पसंद हैं।"
    payload = {
        "memories": [
            {
                "content": claim,
                "category": "preference",
                "importance_score": 6.0,
                "confidence": 0.9,
                "claim_state": "asserted",
                "evidence_turns": [0],
                "evidence_spans": [{"turn_index": 0, "quote": claim}],
                "evidence_relation": "direct_user_statement",
                "proposal_turn": None,
                "reasoning": "Direct user preference.",
            }
        ],
        "memory_clarification": None,
        "nothing_to_extract": False,
        "extraction_notes": None,
    }
    complete = AsyncMock(
        return_value=LLMResponse(
            content=json.dumps(payload, ensure_ascii=False),
            provider_used="test",
            model_used="fake",
            input_tokens=11,
            output_tokens=7,
            total_tokens=18,
            latency_ms=1,
        )
    )
    service = ExtractionService(llm_service=SimpleNamespace(complete=complete))
    result = await service.extract(messages=[{"role": "user", "content": claim}])

    assert result.memories_extracted == 1
    memory = result.memories_to_store[0]
    assert memory.content == claim
    assert memory.validated_evidence["source_spans"][0]["start_char"] == 0
    assert (
        memory.validated_evidence["extraction"]["extractor_version"]
        == "source-evidence-v2"
    )
    assert complete.await_count == 1
