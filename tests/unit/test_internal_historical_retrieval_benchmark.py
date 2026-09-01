import json

from benchmarks.internal.historical_retrieval_eval import DATASET, evaluate_case


def _cases():
    return [json.loads(line) for line in DATASET.read_text(encoding="utf-8").splitlines() if line]


def test_historical_retrieval_pack_is_frozen_and_semantically_competitive() -> None:
    cases = _cases()
    assert len(cases) == 8
    assert len({case["id"] for case in cases}) == 8
    assert all(len(case["candidates"]) >= 3 for case in cases)
    assert all(len(case["relevant_ids"]) >= 1 for case in cases)
    assert all(any(candidate["archived"] for candidate in case["candidates"]) for case in cases)
    assert all(any(not candidate["archived"] for candidate in case["candidates"]) for case in cases)


def test_current_postgres_ranker_is_deterministic_and_leakage_safe() -> None:
    for case in _cases():
        first = evaluate_case(case)
        second = evaluate_case(case)
        assert first["returned_ids"] == second["returned_ids"]
        assert first["current_state_leakage_into_historical"] == 0
        assert first["historical_state_leakage_into_current"] == 0
        assert first["provenance_preserved"] is True
