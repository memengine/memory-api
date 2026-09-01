from __future__ import annotations

from pathlib import Path

from benchmarks.internal.cases import load_cases


ROOT = Path(__file__).resolve().parents[2]
PACK = ROOT / "benchmarks" / "internal" / "datasets" / "extraction" / "development" / "generalization_v1.jsonl"


def test_generalization_pack_has_independent_balanced_coverage() -> None:
    cases = load_cases(PACK)
    tags = [tag for case in cases for tag in case.tags]
    categories = {memory.category for case in cases for memory in case.expected_memories}

    assert len(cases) == 22
    assert len({case.id for case in cases}) == 22
    assert all(case.split == "development" for case in cases)
    assert all("generalization" in case.tags for case in cases)
    assert categories == {"expertise", "fact", "goal", "preference", "procedure", "relationship"}
    assert tags.count("expertise-level") == 5
    assert tags.count("goal-commitment") == 5
    assert tags.count("procedure-durability") == 4
    assert tags.count("category-boundary") == 5
    assert all(memory.evidence_turns for case in cases for memory in case.expected_memories)


def test_generalization_pack_spans_importance_and_disposition() -> None:
    cases = load_cases(PACK)
    memories = [memory for case in cases for memory in case.expected_memories]

    assert min(memory.importance_min for memory in memories) == 1
    assert max(memory.importance_max for memory in memories) == 9
    assert {memory.disposition for memory in memories} == {"store", "pending"}
    assert any(len(case.expected_memories) > 1 for case in cases)
