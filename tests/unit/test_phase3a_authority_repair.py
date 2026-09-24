import hashlib
import json
from types import SimpleNamespace

import pytest

from api.services.evidence_policy import validate_conversational_evidence
from api.services.extraction_service import ExtractionService


def _transcript(user_text: str, *, source_kind: str = "direct_user_input"):
    proposal = "I can remember that you prefer concise troubleshooting answers."
    proposal_hash = hashlib.sha256(proposal.encode()).hexdigest()
    messages = [
        {
            "role": "assistant",
            "content": proposal,
            "source_kind": "assistant_output",
            "is_memory_proposal": True,
            "turn_id": "proposal-turn-1",
            "turn_content_sha256": proposal_hash,
        },
        {
            "role": "user",
            "content": user_text,
            "source_kind": source_kind,
            "turn_id": "user-turn-2",
            "turn_content_sha256": hashlib.sha256(user_text.encode()).hexdigest(),
        },
    ]
    active = [
        {
            "id": "proposal-id-1",
            "group_id": "group-1",
            "ordinal": 1,
            "turn_index": 0,
            "turn_id": "proposal-turn-1",
            "content_sha256": proposal_hash,
        }
    ]
    return messages, active


def test_proposal_policy_derives_user_evidence_from_trusted_transcript() -> None:
    messages, active = _transcript("Yes, remember that.")

    decision = validate_conversational_evidence(
        messages=messages,
        evidence_turns=[0],
        evidence_relation="user_confirmed_assistant_proposal",
        proposal_turn=0,
        visible_turn_indexes={0, 1},
        proposal_confirmation_enabled=True,
        active_proposals=active,
    )

    assert decision.accepted is True
    assert decision.user_turn_indexes == (1,)
    assert decision.proposal_id == "proposal-id-1"


@pytest.mark.parametrize(
    "user_text",
    [
        "No, do not remember that.",
        "Did I say that was my preference?",
        "Nahi, ye yaad mat rakhna.",
        "नहीं, इसे याद मत रखिए।",
    ],
)
def test_explicit_negative_or_question_cannot_confirm_proposal(user_text: str) -> None:
    messages, active = _transcript(user_text)

    decision = validate_conversational_evidence(
        messages=messages,
        evidence_turns=[0],
        evidence_relation="user_confirmed_assistant_proposal",
        proposal_turn=0,
        visible_turn_indexes={0, 1},
        proposal_confirmation_enabled=True,
        active_proposals=active,
    )

    assert decision.accepted is False
    assert decision.reason == "explicit_confirmation_denied"


@pytest.mark.parametrize(
    "user_text",
    [
        "Reject that suggestion. I prefer conclusions first.",
        "That is not my default; I actually prefer detailed troubleshooting steps.",
        "Suggestion reject karo; meri preference conclusion pehle dekhna hai.",
        "उस सुझाव को अस्वीकार करें; मेरी पसंद पहले निष्कर्ष देखने की है।",
        "उस प्रस्ताव को न रखें; मुझे संक्षिप्त उत्तर पसंद हैं।",
    ],
)
def test_explicit_rejection_with_correction_cannot_confirm_proposal(
    user_text: str,
) -> None:
    messages, active = _transcript(user_text)

    decision = validate_conversational_evidence(
        messages=messages,
        evidence_turns=[0, 1],
        evidence_relation="user_confirmed_assistant_proposal",
        proposal_turn=0,
        visible_turn_indexes={0, 1},
        proposal_confirmation_enabled=True,
        active_proposals=active,
    )

    assert decision.accepted is False
    assert decision.reason == "explicit_confirmation_denied"


@pytest.mark.parametrize(
    ("user_text", "proposal_turn", "expected_ordinal"),
    [
        ("Pehla proposal choose karke yaad rakhna.", 0, 1),
        ("Former suggestion ko retain karna hai.", 0, 1),
        ("प्रस्ताव 2 को मेरी सामान्य पसंद बनाइए।", 1, 2),
    ],
)
def test_explicit_multilingual_reference_resolves_registered_proposal(
    user_text: str,
    proposal_turn: int,
    expected_ordinal: int,
) -> None:
    first_messages, _ = _transcript(user_text)
    second_proposal = (
        "I can remember that you prefer detailed troubleshooting answers."
    )
    second_hash = hashlib.sha256(second_proposal.encode()).hexdigest()
    messages = [
        first_messages[0],
        {
            "role": "assistant",
            "content": second_proposal,
            "source_kind": "assistant_output",
            "is_memory_proposal": True,
            "turn_id": "proposal-turn-2",
            "turn_content_sha256": second_hash,
        },
        first_messages[1],
    ]
    active = [
        {
            "id": "proposal-id-1",
            "group_id": "group-1",
            "ordinal": 1,
            "turn_index": 0,
            "turn_id": "proposal-turn-1",
            "content_sha256": messages[0]["turn_content_sha256"],
        },
        {
            "id": "proposal-id-2",
            "group_id": "group-1",
            "ordinal": 2,
            "turn_index": 1,
            "turn_id": "proposal-turn-2",
            "content_sha256": second_hash,
        },
    ]

    decision = validate_conversational_evidence(
        messages=messages,
        evidence_turns=[proposal_turn, 2],
        evidence_relation="user_confirmed_assistant_proposal",
        proposal_turn=proposal_turn,
        visible_turn_indexes={0, 1, 2},
        proposal_confirmation_enabled=True,
        active_proposals=active,
    )

    assert decision.accepted is True
    assert decision.proposal_ordinal == expected_ordinal


def test_unicode_user_statement_can_ground_unicode_memory() -> None:
    user_text = "मुझे व्याख्या से पहले कोड उदाहरण पसंद हैं।"
    candidate = SimpleNamespace(
        content="उपयोगकर्ता को व्याख्या से पहले कोड उदाहरण पसंद हैं।"
    )

    evidence = ExtractionService._validated_user_evidence(
        candidate,
        [{"role": "user", "content": user_text}],
        [0],
        "direct_user_statement",
    )

    assert evidence["relation"] == "direct_user_statement"
    assert evidence["user_turn_indexes"] == [0]
    assert "व्याख्या" in ExtractionService._significant_tokens(user_text)
    assert "उदाहरण" in ExtractionService._significant_tokens(user_text)


def test_tool_result_cannot_be_derived_as_user_confirmation() -> None:
    messages, active = _transcript("Yes, remember that.", source_kind="tool_result")

    decision = validate_conversational_evidence(
        messages=messages,
        evidence_turns=[0],
        evidence_relation="user_confirmed_assistant_proposal",
        proposal_turn=0,
        visible_turn_indexes={0, 1},
        proposal_confirmation_enabled=True,
        active_proposals=active,
    )

    assert decision.accepted is False
    assert decision.reason == "no_user_evidence"


class _DirectRelationLLM:
    async def complete(self, **_kwargs):
        return SimpleNamespace(
            content=json.dumps(
                {
                    "memories": [
                        {
                            "content": "User prefers concise troubleshooting answers.",
                            "category": "preference",
                            "importance_score": 6,
                            "confidence": 0.9,
                            "evidence_turns": [0],
                            "evidence_relation": "direct_user_statement",
                            "proposal_turn": 0,
                            "reasoning": "The user accepted the active proposal.",
                        }
                    ],
                    "nothing_to_extract": False,
                }
            ),
            total_tokens=10,
            provider_used="test",
            model_used="test",
            input_tokens=5,
            output_tokens=5,
            latency_ms=1,
        )


class _SequencedLLM:
    def __init__(self, payloads: list[dict]) -> None:
        self.payloads = payloads
        self.calls: list[dict] = []

    async def complete(self, **kwargs):
        self.calls.append(kwargs)
        payload = self.payloads[len(self.calls) - 1]
        return SimpleNamespace(
            content=json.dumps(payload, ensure_ascii=False),
            total_tokens=10,
            provider_used="test",
            model_used="test",
            input_tokens=5,
            output_tokens=5,
            latency_ms=1,
        )


@pytest.mark.asyncio
async def test_explicit_proposal_turn_uses_stricter_relation() -> None:
    messages, active = _transcript("Yes, remember that.")
    service = ExtractionService(
        llm_service=_DirectRelationLLM(),
        proposal_confirmation_enabled=True,
        importance_shadow_enabled=False,
        app_env="test",
    )

    result = await service.extract(messages=messages, proposal_context=active)

    assert result.memories_extracted == 1
    assert result.memories_to_store[0].validated_evidence["relation"] == (
        "user_confirmed_assistant_proposal"
    )
    assert result.memories_to_store[0].validated_evidence["user_turn_indexes"] == [1]


@pytest.mark.asyncio
async def test_rejected_proposal_candidate_can_only_fall_back_to_direct_grounding() -> None:
    messages, active = _transcript(
        "Reject that suggestion. My preference is to see the conclusion first."
    )
    llm = _SequencedLLM(
        [
            {
                "memories": [
                    {
                        "content": "User prefers to see the conclusion first.",
                        "category": "preference",
                        "importance_score": 6,
                        "confidence": 0.9,
                        "evidence_turns": [0, 1],
                        "evidence_relation": "user_confirmed_assistant_proposal",
                        "proposal_turn": 0,
                        "reasoning": "The user stated a replacement preference.",
                    }
                ],
                "nothing_to_extract": False,
            }
        ]
    )
    service = ExtractionService(
        llm_service=llm,
        proposal_confirmation_enabled=True,
        importance_shadow_enabled=False,
        app_env="test",
    )

    result = await service.extract(messages=messages, proposal_context=active)

    assert len(llm.calls) == 1
    assert result.memories_extracted == 1
    assert result.memories_to_store[0].validated_evidence["relation"] == (
        "direct_user_statement"
    )
    assert result.extraction_metadata["correction_recovery"]["attempted"] is False


@pytest.mark.asyncio
async def test_correction_recovery_runs_only_after_bounded_rejection_miss() -> None:
    messages, active = _transcript(
        "उस सुझाव को अस्वीकार करें; मेरी पसंद पहले निष्कर्ष देखने की है।"
    )
    llm = _SequencedLLM(
        [
            {
                "memories": [],
                "nothing_to_extract": True,
                "extraction_notes": "No accepted proposal.",
            },
            {
                "memories": [
                    {
                        "content": "उपयोगकर्ता की पसंद पहले निष्कर्ष देखने की है।",
                        "category": "preference",
                        "importance_score": 6,
                        "confidence": 0.9,
                        # The model copied a wrong schema-example index. The
                        # server binds this recovery-only citation to turn 1.
                        "evidence_turns": [0],
                        "evidence_relation": "direct_user_statement",
                        "proposal_turn": None,
                        "reasoning": "The user directly stated a replacement preference.",
                    }
                ],
                "nothing_to_extract": False,
            },
        ]
    )
    service = ExtractionService(
        llm_service=llm,
        proposal_confirmation_enabled=True,
        importance_shadow_enabled=False,
        app_env="test",
    )

    result = await service.extract(messages=messages, proposal_context=active)

    assert len(llm.calls) == 2
    assert "recovering one direct replacement memory" in llm.calls[1]["system_prompt"]
    assert "confidence >= 0.80" in llm.calls[1]["system_prompt"]
    assert llm.calls[1]["user_message"].startswith("[turn 1][user]")
    assert "[assistant]" not in llm.calls[1]["user_message"]
    assert result.memories_extracted == 1
    assert result.nothing_to_extract is False
    assert result.memories_to_store[0].validated_evidence["turn_indexes"] == [1]
    assert result.memories_to_store[0].validated_evidence["user_turn_indexes"] == [1]
    assert result.extraction_metadata["correction_recovery"] == {
        "attempted": True,
        "completed": True,
        "accepted_for_storage": 1,
        "accepted_as_pending": 0,
        "provider": "test",
        "model": "test",
        "input_tokens": 5,
        "output_tokens": 5,
        "total_tokens": 10,
        "latency_ms": 1,
        "wall_latency_ms": pytest.approx(0, abs=10),
        "error": None,
    }


@pytest.mark.asyncio
async def test_plain_proposal_rejection_does_not_trigger_correction_recovery() -> None:
    messages, active = _transcript("Reject that proposal; do not remember it.")
    llm = _SequencedLLM(
        [
            {
                "memories": [],
                "nothing_to_extract": True,
                "extraction_notes": "The user rejected the proposal.",
            }
        ]
    )
    service = ExtractionService(
        llm_service=llm,
        proposal_confirmation_enabled=True,
        importance_shadow_enabled=False,
        app_env="test",
    )

    result = await service.extract(messages=messages, proposal_context=active)

    assert len(llm.calls) == 1
    assert result.memories_extracted == 0
    assert result.extraction_metadata["correction_recovery"]["attempted"] is False
