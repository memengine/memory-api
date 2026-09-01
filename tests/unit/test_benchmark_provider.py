from __future__ import annotations

import json

import pytest

from api.infra.benchmark_provider import benchmark_provider_enabled
from api.infra.benchmark_provider import deterministic_completion
from api.infra.benchmark_provider import deterministic_embedding
from api.services.llm_service import LLMService


def test_benchmark_provider_fails_closed_without_isolation(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MEMORYOS_BENCHMARK_PROVIDER", "deterministic")
    monkeypatch.delenv("MEMORYOS_SCALE_DEDICATED", raising=False)
    monkeypatch.setenv("APP_ENV", "benchmark")
    with pytest.raises(RuntimeError, match="SCALE_DEDICATED"):
        benchmark_provider_enabled()


def test_benchmark_provider_requires_benchmark_app_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MEMORYOS_BENCHMARK_PROVIDER", "deterministic")
    monkeypatch.setenv("MEMORYOS_SCALE_DEDICATED", "1")
    monkeypatch.setenv("APP_ENV", "development")
    with pytest.raises(RuntimeError, match="APP_ENV=benchmark"):
        benchmark_provider_enabled()


def test_fixture_is_deterministic_and_exercises_pending(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MEMORYOS_BENCHMARK_PROVIDER", "deterministic")
    monkeypatch.setenv("MEMORYOS_SCALE_DEDICATED", "1")
    monkeypatch.setenv("APP_ENV", "benchmark")
    monkeypatch.setenv("BENCHMARK_EMBED_LATENCY_MS", "0")
    first = deterministic_embedding("prefers concise Python", 32)
    assert first == deterministic_embedding("prefers concise Python", 32)
    assert len(first) == 32
    payload = json.loads(deterministic_completion("extract", "[user]: I might prefer audio, but I am not sure yet.")["content"])
    assert payload["memories"][0]["category"] == "preference"
    assert payload["memories"][0]["confidence"] == 0.55


def test_fixture_returns_update_for_explicit_correction(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MEMORYOS_BENCHMARK_PROVIDER", "deterministic")
    monkeypatch.setenv("MEMORYOS_SCALE_DEDICATED", "1")
    monkeypatch.setenv("APP_ENV", "benchmark")
    payload = json.loads(deterministic_completion("MemoryOS conflict resolution engine", "Correction: use Rust replacing Go")["content"])
    assert payload["action"] == "UPDATE"


@pytest.mark.asyncio
async def test_llm_service_uses_fixture_without_provider_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MEMORYOS_BENCHMARK_PROVIDER", "deterministic")
    monkeypatch.setenv("MEMORYOS_SCALE_DEDICATED", "1")
    monkeypatch.setenv("APP_ENV", "benchmark")
    monkeypatch.setenv("BENCHMARK_EXTRACT_LATENCY_MS", "0")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    service = LLMService(provider_clients={}, require_provider=True, use_state_store=False)
    response = await service.complete("extract", "[user]: I prefer concise Python explanations.")
    assert response.provider_used == "benchmark-deterministic"
    assert json.loads(response.content)["memories"][0]["category"] == "preference"
