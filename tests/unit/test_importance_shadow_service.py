from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

from api.services.extraction_service import ExtractionService
from api.services.importance_shadow_service import ImportanceShadowService
from api.services.llm_service import LLMResponse


class FakeLLM:
    async def complete(self, **kwargs):
        del kwargs
        return LLMResponse(
            content=json.dumps({
                "memories": [{
                    "content": "User prefers diagrams before explanations",
                    "category": "preference",
                    "importance_score": 8.0,
                    "confidence": 0.9,
                    "reasoning": "Explicit preference",
                }],
                "nothing_to_extract": False,
            }),
            provider_used="test",
            model_used="fake",
            input_tokens=1,
            output_tokens=1,
            total_tokens=2,
            latency_ms=1,
        )


class FailingShadowObserver:
    def observe(self, **kwargs):
        del kwargs
        raise RuntimeError("observer unavailable")


def _spec(tmp_path: Path) -> Path:
    path = tmp_path / "extraction_spec.md"
    path.write_text(
        "## 1. Memory Categories\n### PREFERENCE\n**Definition:** preferences\n---\n"
        "## 2. Importance Scoring Rubric\n1 low, 9 high\n"
        "## 3. Example Conversations\nexample\n"
        "## 4. What Should NEVER Be Stored\n**Rule 1 - Secrets**\nNever store secrets.\n---\n"
        "## 5. Edge Cases\n",
        encoding="utf-8",
    )
    return path


@pytest.mark.asyncio
async def test_development_shadow_logs_comparison_without_mutating_result(tmp_path: Path, caplog) -> None:
    service = ExtractionService(
        llm_service=FakeLLM(),
        spec_path=_spec(tmp_path),
        importance_shadow_enabled=True,
        app_env="development",
    )
    with caplog.at_level(logging.INFO, logger="memoryos.importance_shadow"):
        result = await service.extract(
            messages=[{"role": "user", "content": "I prefer diagrams before explanations."}],
            proxy_user_id="proxy",
            tenant_id="tenant",
            job_id="job",
        )

    assert result.memories_to_store[0].importance_score == 8.0
    record = next(record for record in caplog.records if record.msg == "importance_shadow_comparison")
    assert record.provider_calls == 0
    assert record.comparisons[0]["model_score"] == 8.0
    assert record.comparisons[0]["shadow_score"] == 7.0
    assert "content" not in record.comparisons[0]


@pytest.mark.asyncio
async def test_shadow_is_hard_disabled_outside_development(tmp_path: Path, caplog) -> None:
    service = ExtractionService(
        llm_service=FakeLLM(),
        spec_path=_spec(tmp_path),
        importance_shadow_enabled=True,
        app_env="production",
    )
    with caplog.at_level(logging.INFO):
        result = await service.extract(
            messages=[{"role": "user", "content": "I prefer diagrams before explanations."}],
        )

    assert result.memories_to_store[0].importance_score == 8.0
    assert "importance_shadow_comparison" not in caplog.text


@pytest.mark.asyncio
async def test_shadow_failure_is_fail_open_and_preserves_result(tmp_path: Path, caplog) -> None:
    service = ExtractionService(
        llm_service=FakeLLM(),
        spec_path=_spec(tmp_path),
        importance_shadow_enabled=True,
        app_env="development",
        importance_shadow_service=FailingShadowObserver(),
    )
    with caplog.at_level(logging.WARNING):
        result = await service.extract(
            messages=[{"role": "user", "content": "I prefer diagrams before explanations."}],
        )

    assert result.memories_to_store[0].importance_score == 8.0
    assert "importance_shadow_failed" in caplog.text


@pytest.mark.asyncio
async def test_development_review_capture_contains_evidence_but_does_not_mutate(tmp_path: Path) -> None:
    review_dir = tmp_path / "review-captures"
    observer = ImportanceShadowService(review_dir=review_dir)
    service = ExtractionService(
        llm_service=FakeLLM(),
        spec_path=_spec(tmp_path),
        importance_shadow_enabled=True,
        app_env="development",
        importance_shadow_service=observer,
    )
    messages = [{"role": "user", "content": "I prefer diagrams before explanations."}]
    result = await service.extract(messages=messages, job_id="natural-job")

    capture_path = next(review_dir.glob("*.json"))
    capture = json.loads(capture_path.read_text(encoding="utf-8"))
    assert capture["messages"] == messages
    assert capture["memories"][0]["model_score"] == 8.0
    assert capture["memories"][0]["deterministic_score"] == 7.0
    assert capture["telemetry"]["observer_status"] == "success"
    assert capture["telemetry"]["observer_latency_ms"] >= 0
    assert capture["telemetry"]["provider_calls"] == 0
    assert capture["telemetry"]["fallback_count"] == 0
    assert capture["telemetry"]["active_scores_unchanged"] is True
    telemetry_path = next((review_dir / "_telemetry").glob("*.json"))
    telemetry = json.loads(telemetry_path.read_text(encoding="utf-8"))
    assert telemetry["status"] == "success"
    assert telemetry["active_scores_unchanged"] is True
    assert result.memories_to_store[0].importance_score == 8.0

class PersistentlyFailingShadowObserver(ImportanceShadowService):
    def observe(self, **kwargs):
        del kwargs
        raise RuntimeError("observer unavailable")


@pytest.mark.asyncio
async def test_shadow_failure_persists_fail_open_telemetry(tmp_path: Path) -> None:
    review_dir = tmp_path / "review-captures"
    observer = PersistentlyFailingShadowObserver(review_dir=review_dir)
    service = ExtractionService(
        llm_service=FakeLLM(),
        spec_path=_spec(tmp_path),
        importance_shadow_enabled=True,
        app_env="development",
        importance_shadow_service=observer,
    )

    result = await service.extract(
        messages=[{"role": "user", "content": "I prefer diagrams before explanations."}],
        job_id="failed-shadow-job",
    )

    telemetry_path = next((review_dir / "_telemetry").glob("*.json"))
    telemetry = json.loads(telemetry_path.read_text(encoding="utf-8"))
    assert telemetry["status"] == "failure"
    assert telemetry["fallback_count"] == 1
    assert telemetry["active_scores_unchanged"] is True
    assert telemetry["error_type"] == "RuntimeError"
    assert result.memories_to_store[0].importance_score == 8.0
