from __future__ import annotations

from benchmarks.internal.deterministic_importance import DeterministicImportanceScorer
from benchmarks.internal.metrics import _in_range


def _score(content: str, category: str) -> float:
    return DeterministicImportanceScorer().score(
        {"content": content, "category": category},
        [],
    ).score


def test_deterministic_importance_uses_general_rubric_features() -> None:
    assert _score("User may consider changing roles later", "goal") == 3.0
    assert _score("User wants this fixed today", "goal") <= 4.0
    assert _score("User's manager is Morgan", "relationship") >= 6.0
    assert _score("User is a beginner with distributed systems", "expertise") <= 4.0
    assert _score("User has twelve years of professional experience and leads architecture reviews", "expertise") >= 7.0
    assert _score("User lives in Toronto", "fact") == 7.0

def test_feature_derivation_uses_only_supporting_evidence() -> None:
    result = DeterministicImportanceScorer().score(
        {"content": "User refreshes the cache", "category": "procedure", "evidence_turns": [1]},
        [{"role": "user", "content": "Unrelated permanent preference."}, {"role": "user", "content": "For the next two days, refresh manually until the patch ships."}],
    )
    assert result.features.temporal_scope == -2
    assert result.features.procedure_durability_consequence == -2
    assert result.contributions["category_prior"] <= .5
    assert result.score <= 3

def test_feature_derivation_localizes_composite_evidence() -> None:
    scorer = DeterministicImportanceScorer()
    messages = [{"role": "user", "content": "I am in Chennai for a conference this week, but I normally live in Kochi."}]
    temporary = scorer.score({"content": "User is in Chennai for a conference this week", "category": "fact", "evidence_turns": [0]}, messages)
    durable = scorer.score({"content": "User normally lives in Kochi", "category": "fact", "evidence_turns": [0]}, messages)
    assert temporary.features.temporal_scope == -2
    assert durable.features.temporal_scope == 1

def test_general_phrase_families_are_not_case_specific() -> None:
    assert _score("User is comfortable with container orchestration", "expertise") >= 7
    assert _score("User prefers diagrams before prose", "preference") >= 7
    assert _score("User usually reviews reports before morning meetings", "procedure") <= 5

def test_ephemeral_scope_dominates_durable_preference_signal() -> None:
    assert _score("User prefers diagrams for this session", "preference") == 1

def test_pending_disposition_caps_durable_score() -> None:
    result = DeterministicImportanceScorer().score(
        {"content": "User may prefer weekly diagrams", "category": "preference", "disposition": "pending"}, []
    )
    assert result.score == 6

def test_interaction_anchors_and_half_up_rounding_are_transparent() -> None:
    result = DeterministicImportanceScorer().score(
        {"content": "User leads the platform team", "category": "fact"}, []
    )
    assert result.contributions["stable_identity_anchor"] == 1
    assert result.contributions["response_shaping_consequence_anchor"] == 0
    assert result.score == 7

def test_recurring_low_consequence_procedure_stays_low() -> None:
    assert _score("User usually reviews routine notes before morning meetings", "procedure") == 3

def test_recurring_role_context_does_not_double_count_anchors() -> None:
    assert _score("User normally works as a product lead at a software company", "fact") == 7

def test_general_hypothetical_goal_language_retains_low_commitment() -> None:
    assert _score("User is considering a career change but has no concrete plan", "goal") == 3

def test_integer_score_does_not_widen_legacy_half_point_interval() -> None:
    assert _in_range(7, 6.5, 7.5)
    assert not _in_range(8, 6.5, 7.5)


def test_deterministic_importance_does_not_mutate_memory() -> None:
    memory = {"content": "User is a designer", "category": "fact", "confidence": 0.9}
    original = dict(memory)

    DeterministicImportanceScorer().score(memory, [])

    assert memory == original
