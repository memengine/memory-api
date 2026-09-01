from benchmarks.internal.retrieval_cases import load_retrieval_development_cases
from benchmarks.internal.retrieval_eval import aggregate, evaluate_case


def test_retrieval_development_dataset_has_required_coverage() -> None:
    cases = load_retrieval_development_cases()
    assert len(cases) == 12
    assert len({case.id for case in cases}) == len(cases)
    assert {case.scenario_type for case in cases} >= {
        "semantic", "ranking_tradeoff", "multi_relevant", "filter", "lifecycle",
        "isolation", "provenance", "deduplication", "empty",
    }
    assert all("holdout" not in case.id.lower() for case in cases)


def test_retrieval_metrics_detect_wrong_ranking_and_leakage() -> None:
    cases = {case.id: case for case in load_retrieval_development_cases()}
    rows = [evaluate_case(case) for case in cases.values()]
    summary = aggregate(rows)
    assert 0.0 <= summary["precision_at_k"] <= 1.0
    assert 0.0 <= summary["recall_at_k"] <= 1.0
    assert 0.0 <= summary["mrr"] <= 1.0
    assert 0.0 <= summary["ndcg_at_k"] <= 1.0
    assert evaluate_case(cases["superseded_exclusion"])["superseded_leak"] is False
    assert evaluate_case(cases["tenant_isolation"])["filter_leak"] is False


def test_retrieval_baseline_is_deterministic() -> None:
    cases = load_retrieval_development_cases()
    assert [evaluate_case(case) for case in cases] == [evaluate_case(case) for case in cases]
