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
