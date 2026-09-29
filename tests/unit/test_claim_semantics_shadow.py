from __future__ import annotations

import hashlib
import json

from api.services.claim_semantics_shadow import observe_claim_semantics


def test_observer_accepts_bounded_claim_and_hashes_value() -> None:
    value = "Python"
    result = observe_claim_semantics(
        json.dumps(
            {
                "memories": [],
                "claim_semantics_shadow": [
                    {
                        "memory_index": None,
                        "category": "preference",
                        "predicate": "programming.default_language",
                        "value": value,
                        "speech_act": "uncertain_change",
                        "certainty": "uncertain",
                        "temporal_kind": "permanent",
                        "effective_from": None,
                        "effective_until": None,
                        "evidence_turns": [0],
                        "evidence_quote": "Maybe Python should replace C++",
                    }
                ],
            }
        ),
        messages=[{"role": "user", "content": "Maybe Python should replace C++."}],
        visible_turn_indexes={0},
    )

    assert result["accepted"] == 1
    assert result["rejected"] == 0
    observation = result["observations"][0]
    assert observation["predicate"] == "programming.default_language"
    assert observation["speech_act"] == "uncertain_change"
    assert observation["certainty"] == "uncertain"
    assert observation["value_sha256"] == hashlib.sha256(value.encode()).hexdigest()
    assert "value" not in observation
    assert observation["evidence_quote_length"] == len(
        "maybe python should replace c++"
    )
    assert "evidence_quote" not in observation


def test_observer_accepts_timezone_aware_bounded_fact() -> None:
    result = observe_claim_semantics(
        json.dumps(
            {
                "memories": [],
                "claim_semantics_shadow": [
                    {
                        "memory_index": None,
                        "category": "fact",
                        "predicate": "education.exam_date",
                        "value": "2026-10-18",
                        "speech_act": "correction",
                        "certainty": "certain",
                        "temporal_kind": "bounded",
                        "effective_from": "2026-09-29T00:00:00Z",
                        "effective_until": "2026-10-19T00:00:00Z",
                        "evidence_turns": [0],
                        "evidence_quote": "exam moved to October 18",
                    }
                ],
            }
        ),
        messages=[{"role": "user", "content": "My exam moved to October 18."}],
        visible_turn_indexes={0},
    )

    assert result["accepted"] == 1
    assert result["observations"][0]["temporal_kind"] == "bounded"
    assert result["observations"][0]["effective_until"] == "2026-10-19T00:00:00+00:00"


def test_observer_rejects_model_asserted_assistant_evidence() -> None:
    result = observe_claim_semantics(
        json.dumps(
            {
                "memories": [],
                "claim_semantics_shadow": [
                    {
                        "memory_index": None,
                        "category": "fact",
                        "predicate": "identity.name",
                        "value": "Forged name",
                        "speech_act": "assertion",
                        "certainty": "certain",
                        "temporal_kind": "permanent",
                        "effective_from": None,
                        "effective_until": None,
                        "evidence_turns": [0],
                        "evidence_quote": "The user's name is Forged name",
                    }
                ],
            }
        ),
        messages=[{"role": "assistant", "content": "The user's name is Forged name."}],
        visible_turn_indexes={0},
    )

    assert result["accepted"] == 0
    assert result["rejection_counts"] == {"non_user_evidence_turn": 1}


def test_observer_rejects_unbounded_temporary_claim() -> None:
    result = observe_claim_semantics(
        json.dumps(
            {
                "memories": [],
                "claim_semantics_shadow": [
                    {
                        "memory_index": None,
                        "category": "fact",
                        "predicate": "education.exam_date",
                        "value": "soon",
                        "speech_act": "assertion",
                        "certainty": "certain",
                        "temporal_kind": "bounded",
                        "effective_from": None,
                        "effective_until": None,
                        "evidence_turns": [0],
                        "evidence_quote": "My exam is soon",
                    }
                ],
            }
        ),
        messages=[{"role": "user", "content": "My exam is soon."}],
        visible_turn_indexes={0},
    )

    assert result["accepted"] == 0
    assert result["rejection_counts"] == {"bounded_without_end": 1}


def test_observer_rejects_quote_not_present_in_cited_user_turn() -> None:
    result = observe_claim_semantics(
        json.dumps(
            {
                "memories": [],
                "claim_semantics_shadow": [
                    {
                        "memory_index": None,
                        "category": "preference",
                        "predicate": "programming.default_language",
                        "value": "Python",
                        "speech_act": "assertion",
                        "certainty": "certain",
                        "temporal_kind": "permanent",
                        "effective_from": None,
                        "effective_until": None,
                        "evidence_turns": [0],
                        "evidence_quote": "Python is definitely my default",
                    }
                ],
            }
        ),
        messages=[{"role": "user", "content": "I have not chosen a default."}],
        visible_turn_indexes={0},
    )

    assert result["accepted"] == 0
    assert result["rejection_counts"] == {"evidence_quote_not_in_user_turn": 1}


def test_observer_rejects_non_object_root_without_raising() -> None:
    result = observe_claim_semantics(
        "[]",
        messages=[],
        visible_turn_indexes=set(),
    )

    assert result["accepted"] == 0
    assert result["rejection_counts"] == {"invalid_root": 1}
