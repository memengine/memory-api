from collections import Counter
from pathlib import Path

from benchmarks.internal.cases import ALLOWED_CATEGORIES
from benchmarks.internal.phase3a_confirmation import PROPOSALS, load_development_cases

ROOT = Path(__file__).resolve().parents[2]
DATASET = (
    ROOT
    / "benchmarks"
    / "internal"
    / "datasets"
    / "phase3a_confirmation"
    / "development"
    / "proposal_generalization_v1.json"
)
REFERENCE_TYPES = {
    "single_vague",
    "single_indirect",
    "explicit_ordinal",
    "ambiguous_multi",
    "rejection",
}


def _normalized_pair(content: str, memory: str) -> tuple[str, str]:
    return content.casefold().strip(), memory.casefold().strip()


def test_phase3a_proposal_generalization_has_balanced_slices() -> None:
    cases, minimums = load_development_cases(DATASET)

    assert len(cases) == 60
    assert minimums == {"language": 20, "reference_type": 12}
    assert Counter(case.language for case in cases) == {
        "en": 20,
        "hi": 20,
        "hinglish": 20,
    }
    assert Counter(case.reference_type for case in cases) == {
        reference_type: 12 for reference_type in REFERENCE_TYPES
    }
    assert Counter(case.expected_outcome for case in cases) == {
        "accepted": 36,
        "pending": 12,
        "rejected": 12,
    }
    assert len({case.utterance.casefold().strip() for case in cases}) == len(cases)


def test_phase3a_proposal_generalization_has_valid_targets_and_shapes() -> None:
    cases, _ = load_development_cases(DATASET)

    for case in cases:
        assert case.proposals is not None
        expected_count = (
            2
            if case.reference_type in {"explicit_ordinal", "ambiguous_multi"}
            else 1
        )
        assert len(case.proposals) == expected_count
        if case.expected_outcome == "accepted":
            assert case.target_ordinal is not None
            assert 1 <= case.target_ordinal <= expected_count
        else:
            assert case.target_ordinal is None


def test_phase3a_proposal_generalization_is_diverse_and_independent() -> None:
    cases, _ = load_development_cases(DATASET)
    proposals = [proposal for case in cases for proposal in case.proposals or ()]
    proposal_pairs = [
        _normalized_pair(proposal.content, proposal.memory) for proposal in proposals
    ]
    legacy_pairs = {
        _normalized_pair(proposal["content"], proposal["memory"])
        for proposal in PROPOSALS
    }

    assert len(proposals) == 84
    assert len(set(proposal_pairs)) == len(proposal_pairs)
    assert not legacy_pairs.intersection(proposal_pairs)
    assert Counter(proposal.category for proposal in proposals) == {
        category: 14 for category in ALLOWED_CATEGORIES
    }
