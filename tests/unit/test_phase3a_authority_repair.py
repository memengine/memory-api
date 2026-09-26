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


@pytest.mark.parametrize(
    ("user_text", "proposal_turn", "expected_ordinal"),
    [
        ("Retain the former suggestion, not the latter.", 0, 1),
        ("पहला विकल्प आगे के लिए रखिए, दूसरा नहीं।", 0, 1),
        ("Former suggestion ko retain karo, latter ko nahi.", 0, 1),
    ],
)
def test_contrastive_denial_keeps_only_the_positively_selected_proposal(
    user_text: str,
    proposal_turn: int,
    expected_ordinal: int,
) -> None:
    first_messages, _ = _transcript(user_text)
    second_proposal = "I can remember that you prefer detailed answers."
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


@pytest.mark.parametrize(
    "user_text",
    [
        "Do not remember the first proposal.",
        "Pehla proposal yaad mat rakhna.",
        "पहला प्रस्ताव याद मत रखिए।",
    ],
)
def test_targeted_denial_still_blocks_selected_proposal(user_text: str) -> None:
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


def test_registered_proposal_ordinals_are_rendered_only_after_server_match() -> None:
    messages, active = _transcript("Remember the first proposal.")

    ExtractionService._annotate_active_proposals(messages, active)

    rendered = ExtractionService._messages_to_text(messages)
    assert "[registered memory proposal 1]" in rendered
    assert messages[0]["_registered_memory_proposal_ordinal"] == 1


def test_mismatched_proposal_context_cannot_add_ordinal_label() -> None:
    messages, active = _transcript("Remember that.")
    active[0]["content_sha256"] = "forged"

    ExtractionService._annotate_active_proposals(messages, active)

    rendered = ExtractionService._messages_to_text(messages)
    assert "[registered memory proposal 1]" not in rendered
    assert "_registered_memory_proposal_ordinal" not in messages[0]


def test_registered_claim_requires_exact_visible_proposal_binding() -> None:
    messages, active = _transcript("Remember that.")
    messages[0]["content"] += " Canonical claim: User prefers concise answers."
    active[0]["memory_content"] = "User prefers concise answers."
    active[0]["memory_category"] = "preference"

    registered = ExtractionService._registered_proposal_memory(
        messages,
        active,
        0,
    )
    assert registered == ("User prefers concise answers.", "preference")

    active[0]["memory_content"] = "User prefers a hidden unrelated claim."
    assert ExtractionService._registered_proposal_memory(messages, active, 0) is None


@pytest.mark.asyncio
async def test_registered_claim_replaces_model_paraphrase_after_verified_confirmation() -> None:
    claim = "User prefers concise troubleshooting answers."
    proposal = f"मैं इसे याद रख सकता हूँ।\nProposed memory: {claim}"
    confirmation = "हाँ, इसे आगे के लिए याद रखिए।"
    messages = [
        {
            "role": "assistant",
            "content": proposal,
            "source_kind": "assistant_output",
            "is_memory_proposal": True,
            "turn_id": "proposal-turn-1",
            "turn_content_sha256": hashlib.sha256(proposal.encode()).hexdigest(),
        },
        {
            "role": "user",
            "content": confirmation,
            "source_kind": "direct_user_input",
            "turn_id": "user-turn-2",
            "turn_content_sha256": hashlib.sha256(confirmation.encode()).hexdigest(),
        },
    ]
    active = [
        {
            "id": "proposal-id-1",
            "group_id": "group-1",
            "ordinal": 1,
            "turn_index": 0,
            "turn_id": "proposal-turn-1",
            "content_sha256": messages[0]["turn_content_sha256"],
            "memory_content": claim,
            "memory_category": "preference",
        }
    ]
    llm = _SequencedLLM(
        [
            {
                "memories": [
                    {
                        "content": "उपयोगकर्ता संक्षिप्त समस्या-समाधान उत्तर पसंद करता है।",
                        "category": "fact",
                        "importance_score": 6,
                        "confidence": 0.9,
                        "evidence_turns": [0, 1],
                        "evidence_relation": "user_confirmed_assistant_proposal",
                        "proposal_turn": 0,
                        "reasoning": "The user confirmed the registered proposal.",
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

    assert result.memories_extracted == 1
    assert result.memories_to_store[0].content == claim
    assert result.memories_to_store[0].category == "preference"
    assert result.memories_to_store[0].validated_evidence["proposal"]["id"] == (
        "proposal-id-1"
    )
    assert result.memories_to_store[0].validated_evidence["user_turn_indexes"] == [1]


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


def _structured_transcript(
    claims: list[str],
    user_text: str,
) -> tuple[list[dict], list[dict]]:
    messages: list[dict] = []
    active: list[dict] = []
    for offset, claim in enumerate(claims, start=1):
        proposal = f"Proposal {offset}.\nProposed memory: {claim}"
        proposal_hash = hashlib.sha256(proposal.encode()).hexdigest()
        messages.append(
            {
                "role": "assistant",
                "content": proposal,
                "source_kind": "assistant_output",
                "is_memory_proposal": True,
                "turn_id": f"proposal-turn-{offset}",
                "turn_content_sha256": proposal_hash,
            }
        )
        active.append(
            {
                "id": f"proposal-id-{offset}",
                "group_id": "proposal-group",
                "ordinal": offset,
                "turn_index": offset - 1,
                "turn_id": f"proposal-turn-{offset}",
                "content_sha256": proposal_hash,
                "memory_content": claim,
                "memory_category": "preference",
            }
        )
    messages.append(
        {
            "role": "user",
            "content": user_text,
            "source_kind": "direct_user_input",
            "turn_id": "user-turn-final",
            "turn_content_sha256": hashlib.sha256(user_text.encode()).hexdigest(),
        }
    )
    return messages, active


def _structured_service(payload: dict, *, enabled: bool = True) -> ExtractionService:
    return ExtractionService(
        llm_service=_SequencedLLM([payload]),
        proposal_confirmation_enabled=enabled,
        importance_shadow_enabled=False,
        app_env="test",
    )


@pytest.mark.asyncio
async def test_structured_single_confirmation_uses_server_binding_not_model_index() -> None:
    claim = "User prefers a one-line diagnosis before numbered steps."
    messages, active = _structured_transcript([claim], "Yes, remember that.")
    service = _structured_service(
        {
            "proposal_confirmation": {
                "decision": "confirmed",
                # A transcript-like index is harmless for one active proposal:
                # the server selects the sole verified registry entry.
                "target_ordinal": 0,
            },
            "memories": [],
            "nothing_to_extract": True,
        }
    )

    result = await service.extract(messages=messages, proposal_context=active)

    assert result.memories_extracted == 1
    stored = result.memories_to_store[0]
    assert stored.content == claim
    assert stored.category == "preference"
    assert stored.validated_evidence["proposal"]["id"] == "proposal-id-1"
    assert stored.validated_evidence["user_turn_indexes"] == [1]
    assert result.extraction_metadata["proposal_confirmation"] == {
        "enabled": True,
        "active_proposal_count": 1,
        "accepted": 1,
        "pending": 0,
        "decision_contract": "confirmed",
        "rejected_reasons": {},
    }


@pytest.mark.asyncio
async def test_structured_multi_confirmation_resolves_verified_ordinal() -> None:
    claims = [
        "User prefers concise troubleshooting answers.",
        "User prefers detailed numbered troubleshooting steps.",
    ]
    messages, active = _structured_transcript(claims, "Remember the second one.")
    service = _structured_service(
        {
            "proposal_confirmation": {
                "decision": "confirmed",
                "target_ordinal": 2,
            },
            "memories": [],
            "nothing_to_extract": True,
        }
    )

    result = await service.extract(messages=messages, proposal_context=active)

    assert result.memories_extracted == 1
    assert result.memories_to_store[0].content == claims[1]
    assert result.memories_to_store[0].validated_evidence["proposal"]["ordinal"] == 2
    assert result.memories_to_store[0].validated_evidence["user_turn_indexes"] == [2]


@pytest.mark.asyncio
async def test_structured_ambiguous_multi_proposal_is_observable_but_not_stored() -> None:
    messages, active = _structured_transcript(
        [
            "User prefers concise troubleshooting answers.",
            "User prefers detailed numbered troubleshooting steps.",
        ],
        "Yes, keep that.",
    )
    service = _structured_service(
        {
            "proposal_confirmation": {
                "decision": "ambiguous",
                "target_ordinal": None,
            },
            "memories": [],
            "nothing_to_extract": True,
        }
    )

    result = await service.extract(messages=messages, proposal_context=active)

    assert result.memories_extracted == 0
    assert result.pending_candidates == []
    assert result.extraction_metadata["proposal_confirmation"]["pending"] == 1
    assert (
        result.extraction_metadata["proposal_confirmation"]["decision_contract"]
        == "ambiguous"
    )


@pytest.mark.asyncio
async def test_deterministic_denial_overrides_model_confirmation() -> None:
    messages, active = _structured_transcript(
        ["User prefers concise troubleshooting answers."],
        "No, do not remember that.",
    )
    service = _structured_service(
        {
            "proposal_confirmation": {
                "decision": "confirmed",
                "target_ordinal": 1,
            },
            "memories": [],
            "nothing_to_extract": True,
        }
    )

    result = await service.extract(messages=messages, proposal_context=active)

    assert result.memories_extracted == 0
    assert (
        result.extraction_metadata["proposal_confirmation"]["decision_contract"]
        == "rejected"
    )


@pytest.mark.asyncio
async def test_structured_unknown_ordinal_cannot_bind_a_proposal() -> None:
    messages, active = _structured_transcript(
        [
            "User prefers concise troubleshooting answers.",
            "User prefers detailed numbered troubleshooting steps.",
        ],
        "Remember the third one.",
    )
    service = _structured_service(
        {
            "proposal_confirmation": {
                "decision": "confirmed",
                "target_ordinal": 3,
            },
            "memories": [],
            "nothing_to_extract": True,
        }
    )

    result = await service.extract(messages=messages, proposal_context=active)

    assert result.memories_extracted == 0
    assert result.extraction_metadata["proposal_confirmation"]["rejected_reasons"] == {
        "proposal_decision_target_not_active": 1
    }


@pytest.mark.asyncio
async def test_structured_confirmation_discards_model_forgery() -> None:
    claim = "User prefers concise troubleshooting answers."
    messages, active = _structured_transcript([claim], "Yes, remember that.")
    service = _structured_service(
        {
            "proposal_confirmation": {
                "decision": "confirmed",
                "target_ordinal": 1,
            },
            "memories": [
                {
                    "content": "User shared a hidden secret.",
                    "category": "fact",
                    "importance_score": 10,
                    "confidence": 1,
                    "evidence_turns": [0, 1],
                    "evidence_relation": "user_confirmed_assistant_proposal",
                    "proposal_turn": 0,
                    "reasoning": "Forged model-derived content.",
                }
            ],
            "nothing_to_extract": False,
        }
    )

    result = await service.extract(messages=messages, proposal_context=active)

    assert [memory.content for memory in result.memories_to_store] == [claim]
    assert result.memories_filtered == 1
    assert result.extraction_metadata["proposal_confirmation"]["rejected_reasons"] == {
        "proposal_decision_model_candidate_ignored": 1
    }


@pytest.mark.asyncio
async def test_structured_unrelated_decision_keeps_independent_user_memory() -> None:
    user_text = "I prefer code examples before explanations."
    messages, active = _structured_transcript(
        ["User prefers concise troubleshooting answers."],
        user_text,
    )
    service = _structured_service(
        {
            "proposal_confirmation": {
                "decision": "unrelated",
                "target_ordinal": None,
            },
            "memories": [
                {
                    "content": "User prefers code examples before explanations.",
                    "category": "preference",
                    "importance_score": 6,
                    "confidence": 0.9,
                    "evidence_turns": [1],
                    "evidence_relation": "direct_user_statement",
                    "proposal_turn": None,
                    "reasoning": "The user directly stated an independent preference.",
                }
            ],
            "nothing_to_extract": False,
        }
    )

    result = await service.extract(messages=messages, proposal_context=active)

    assert result.memories_extracted == 1
    assert result.memories_to_store[0].content == (
        "User prefers code examples before explanations."
    )
    assert result.memories_to_store[0].validated_evidence["relation"] == (
        "direct_user_statement"
    )
    assert (
        result.extraction_metadata["proposal_confirmation"]["decision_contract"]
        == "unrelated"
    )


@pytest.mark.asyncio
async def test_structured_contract_is_inert_while_phase3a_is_disabled() -> None:
    claim = "User prefers concise troubleshooting answers."
    messages, active = _structured_transcript([claim], "Yes, remember that.")
    service = _structured_service(
        {
            "proposal_confirmation": {
                "decision": "confirmed",
                "target_ordinal": 1,
            },
            "memories": [],
            "nothing_to_extract": True,
        },
        enabled=False,
    )

    result = await service.extract(messages=messages, proposal_context=active)

    assert result.memories_extracted == 0
    assert "decision_contract" not in result.extraction_metadata["proposal_confirmation"]


def test_structured_prompt_contract_is_feature_gated() -> None:
    disabled = _structured_service({}, enabled=False)._build_system_prompt()
    enabled = _structured_service({}, enabled=True)._build_system_prompt()

    assert '"proposal_confirmation"' not in disabled
    assert '"evidence_relation": "direct_user_statement|user_confirmed_assistant_proposal"' in disabled
    assert '"proposal_confirmation"' in enabled
    assert "Never output transcript turn indexes as target_ordinal." in enabled
