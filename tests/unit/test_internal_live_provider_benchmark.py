from __future__ import annotations

import pytest

from api.services.llm_service import LLMResponse
from benchmarks.internal.live_provider import (
    _error_record,
    _estimate_cost,
    load_development_cases,
)


def test_live_provider_loader_is_development_only() -> None:
    cases = load_development_cases()

    assert len(cases) == 46
    assert all(case.split == "development" for case in cases)


def test_gemini_cost_uses_recorded_model_and_token_types() -> None:
    response = LLMResponse(
        content="{}",
        provider_used="gemini",
        model_used="gemini-2.5-flash",
        input_tokens=1_000_000,
        output_tokens=1_000_000,
        total_tokens=2_000_000,
        latency_ms=10,
    )

    cost, warnings = _estimate_cost([response])

    assert cost == pytest.approx(2.80)
    assert warnings == []


def test_missing_price_is_reported_instead_of_invented() -> None:
    response = LLMResponse(
        content="{}",
        provider_used="unknown",
        model_used="unknown-model",
        input_tokens=100,
        output_tokens=50,
        total_tokens=150,
        latency_ms=10,
    )

    cost, warnings = _estimate_cost([response])

    assert cost == 0.0
    assert warnings == ["missing pricing rate for unknown/unknown-model"]


def test_error_records_keep_failure_domain_explicit() -> None:
    record = _error_record("benchmark_harness_error", ValueError("bad fixture"))

    assert record == {
        "kind": "benchmark_harness_error",
        "type": "ValueError",
        "message": "bad fixture",
    }
