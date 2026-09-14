from __future__ import annotations

import argparse
import asyncio
import json
import time
from collections import Counter
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from api.services.extraction_service import ExtractionError, ExtractionService
from api.services.llm_service import (
    AllProvidersFailedError,
    LLMResponse,
    LLMService,
    ProviderError,
)
from benchmarks.internal.cases import ExtractionCase, load_cases, load_legacy_cases
from benchmarks.internal.metrics import evaluate_extraction
from benchmarks.internal.results import build_run_record, write_run_record

ROOT = Path(__file__).resolve().parents[2]
LEGACY_DEVELOPMENT = ROOT / "tests" / "evals" / "general_extraction_cases"
INTERNAL_DEVELOPMENT = ROOT / "benchmarks" / "internal" / "datasets" / "extraction" / "development"
DEFAULT_ARTIFACT_ROOT = ROOT / "artifacts" / "internal-benchmarks"

# Standard paid-tier text rates in USD per 1M tokens. The rate actually used is
# written into every run artifact so historical cost estimates remain auditable.
MODEL_RATES: dict[tuple[str, str], tuple[float, float, str]] = {
    ("gemini", "gemini-2.5-flash"): (
        0.30,
        2.50,
        "https://ai.google.dev/gemini-api/docs/pricing",
    ),
    ("openai", "gpt-4o-mini"): (
        0.15,
        0.60,
        "https://openai.com/api/pricing/",
    ),
    ("anthropic", "claude-haiku-4-5-20251001"): (
        1.00,
        5.00,
        "https://docs.anthropic.com/en/docs/about-claude/pricing",
    ),
}


class NoopUsageCache:
    async def increment_provider_usage(self, provider: str, hour_bucket: str, ttl: int) -> None:
        del provider, hour_bucket, ttl


class RecordingLLMService:
    def __init__(self, service: LLMService) -> None:
        self.service = service
        self.responses: list[LLMResponse] = []

    async def complete(self, **kwargs: Any) -> LLMResponse:
        response = await self.service.complete(**kwargs)
        self.responses.append(response)
        return response


def load_development_cases() -> list[ExtractionCase]:
    cases = [
        *load_legacy_cases(LEGACY_DEVELOPMENT, split="development"),
        *load_cases(INTERNAL_DEVELOPMENT),
    ]
    if not cases or any(case.split != "development" for case in cases):
        raise RuntimeError("live provider evaluation may load development cases only")
    return cases


async def run_live_development_evaluation() -> dict[str, Any]:
    return await run_live_case_evaluation(
        load_development_cases(),
        mode="live-provider-development-only",
        holdout_loaded=False,
    )


async def run_live_case_evaluation(
    cases: list[ExtractionCase],
    *,
    mode: str,
    holdout_loaded: bool,
) -> dict[str, Any]:
    recorder = RecordingLLMService(LLMService())
    extraction = ExtractionService(
        llm_service=recorder,
        cache_service=NoopUsageCache(),
    )
    metrics = []
    details: list[dict[str, Any]] = []

    for case in cases:
        recorder.responses.clear()
        started = time.perf_counter()
        predictions: list[dict[str, Any]] = []
        error: dict[str, Any] | None = None
        extraction_metadata: dict[str, Any] = {}
        try:
            result = await extraction.extract(
                messages=list(case.messages),
                proxy_user_id=f"internal-benchmark-{case.id}",
                tenant_id="internal-benchmark-development",
                job_id=f"internal-benchmark-{case.id}",
            )
            extraction_metadata = dict(result.extraction_metadata or {})
            predictions.extend(
                {
                    "content": item.content,
                    "category": str(item.category),
                    "disposition": "store",
                    "importance_score": float(item.importance_score),
                    "confidence": float(item.confidence),
                    "reasoning": item.reasoning,
                    "evidence_turns": list(item.validated_evidence.get("turn_indexes") or []),
                }
                for item in result.memories_to_store
            )
            predictions.extend(
                {
                    "content": item.content,
                    "category": str(item.category),
                    "disposition": "pending",
                    "importance_score": float(item.importance_score),
                    "confidence": float(item.confidence),
                    "reasoning": item.reasoning,
                    "evidence_turns": list(item.validated_evidence.get("turn_indexes") or []),
                }
                for item in result.pending_candidates
            )
        except (ProviderError, AllProvidersFailedError) as exc:
            error = _error_record("provider_error", exc)
        except ExtractionError as exc:
            error = _error_record("model_output_error", exc)
        except Exception as exc:
            error = _error_record("benchmark_harness_error", exc)

        latency_ms = (time.perf_counter() - started) * 1000
        calls = [_response_record(response) for response in recorder.responses]
        estimated_cost, pricing_warnings = _estimate_cost(recorder.responses)
        case_metrics = evaluate_extraction(
            case,
            predictions,
            estimated_cost_usd=estimated_cost,
        )
        metrics.append(case_metrics)
        details.append(
            {
                "id": case.id,
                "case_type": case.case_type,
                "tags": list(case.tags),
                "status": "error" if error else "completed",
                "error": error,
                "evidence_source": "production_validated_extraction",
                "latency_ms": round(latency_ms, 3),
                "provider_calls": calls,
                "pricing_warnings": pricing_warnings,
                "extraction_metadata": extraction_metadata,
                "predictions": predictions,
                "metrics": asdict(case_metrics),
            }
        )

    record = build_run_record(
        cases,
        metrics,
        config={
            "mode": mode,
            "holdout_loaded": holdout_loaded,
            "production_extraction_path": "api.services.extraction_service.ExtractionService",
            "pricing_rates_usd_per_1m_tokens": {
                f"{provider}/{model}": {
                    "input": rate[0],
                    "output": rate[1],
                    "source": rate[2],
                }
                for (provider, model), rate in MODEL_RATES.items()
            },
        },
    )
    record["cases"] = details
    _add_live_summary(record, details)
    return record


def _response_record(response: LLMResponse) -> dict[str, Any]:
    return {
        "provider": response.provider_used,
        "model": response.model_used,
        "input_tokens": response.input_tokens,
        "output_tokens": response.output_tokens,
        "total_tokens": response.total_tokens,
        "latency_ms": response.latency_ms,
    }


def _estimate_cost(responses: list[LLMResponse]) -> tuple[float, list[str]]:
    total = 0.0
    warnings: list[str] = []
    for response in responses:
        key = (response.provider_used.lower(), response.model_used)
        rate = MODEL_RATES.get(key)
        if rate is None:
            warnings.append(f"missing pricing rate for {key[0]}/{key[1]}")
            continue
        total += (response.input_tokens / 1_000_000) * rate[0]
        total += (response.output_tokens / 1_000_000) * rate[1]
    return total, warnings


def _error_record(kind: str, exc: Exception) -> dict[str, str]:
    return {
        "kind": kind,
        "type": exc.__class__.__name__,
        "message": str(exc),
    }


def _percentile(values: list[float], ratio: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, round((len(ordered) - 1) * ratio))
    return ordered[index]


def _mean(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def _language_slice(tags: list[str]) -> str:
    for tag in tags:
        if tag.startswith("language:"):
            return tag.split(":", 1)[1] or "unlabeled"
    if "code-switching" in tags:
        return "code-switched"
    return "unlabeled"


def _slice_metrics(cases: list[dict[str, Any]]) -> dict[str, float | int]:
    expected = sum(int(case["metrics"]["expected_count"]) for case in cases)
    predicted = sum(int(case["metrics"]["predicted_count"]) for case in cases)
    matched = sum(int(case["metrics"]["matched_count"]) for case in cases)
    precision = matched / predicted if predicted else 1.0
    recall = matched / expected if expected else 1.0
    return {
        "case_count": len(cases),
        "micro_precision": precision,
        "micro_recall": recall,
        "micro_f1": (2 * precision * recall / (precision + recall)) if precision + recall else 0.0,
        "mean_latency_ms": _mean([float(case["latency_ms"]) for case in cases]),
        "estimated_cost_usd": sum(float(case["metrics"]["estimated_cost_usd"]) for case in cases),
    }


def _add_live_summary(record: dict[str, Any], details: list[dict[str, Any]]) -> None:
    calls = [call for case in details for call in case["provider_calls"]]
    latencies = [float(case["latency_ms"]) for case in details]
    errors = Counter(
        case["error"]["kind"]
        for case in details
        if case["error"] is not None
    )
    completed = [case for case in details if case["status"] == "completed"]
    composition_metrics = [
        dict(case.get("extraction_metadata", {}).get("compositional_pass_metrics") or {})
        for case in completed
    ]
    prompt_metrics = [
        dict(case.get("extraction_metadata", {}).get("prompt_context") or {})
        for case in completed
    ]
    language_slices: dict[str, list[dict[str, Any]]] = {}
    for case in details:
        language_slices.setdefault(_language_slice(list(case["tags"])), []).append(case)
    attempted_cases = [
        case
        for case, metrics in zip(completed, composition_metrics, strict=False)
        if metrics.get("attempted")
    ]
    non_attempted_cases = [
        case
        for case, metrics in zip(completed, composition_metrics, strict=False)
        if not metrics.get("attempted")
    ]
    record["summary"].update(
        {
            "completed_cases": sum(case["status"] == "completed" for case in details),
            "errored_cases": sum(case["status"] == "error" for case in details),
            "errors_by_kind": dict(errors),
            "latency_ms": {
                "mean": sum(latencies) / len(latencies) if latencies else 0.0,
                "p50": _percentile(latencies, 0.50),
                "p95": _percentile(latencies, 0.95),
                "max": max(latencies, default=0.0),
            },
            "token_usage": {
                "input": sum(call["input_tokens"] for call in calls),
                "output": sum(call["output_tokens"] for call in calls),
                "total": sum(call["total_tokens"] for call in calls),
            },
            "provider_models": dict(
                Counter(f"{call['provider']}/{call['model']}" for call in calls)
            ),
            "pricing_warning_count": sum(
                len(case["pricing_warnings"]) for case in details
            ),
            "production_evidence": {
                "source": "validated_extraction_result",
                "additional_attribution_calls": 0,
            },
            "prompt_context": {
                "mean_existing_memory_context_tokens": _mean(
                    [float(item.get("existing_memory_context_tokens", 0)) for item in prompt_metrics]
                ),
                "mean_primary_user_message_tokens": _mean(
                    [float(item.get("primary_user_message_tokens", 0)) for item in prompt_metrics]
                ),
                "mean_existing_memories_included": _mean(
                    [float(item.get("existing_memories_included", 0)) for item in prompt_metrics]
                ),
            },
            "compositional_pass": {
                "attempt_rate": _mean([float(bool(item.get("attempted"))) for item in composition_metrics]),
                "used_rate": _mean([float(bool(item.get("used"))) for item in composition_metrics]),
                "mean_latency_ms_when_attempted": _mean(
                    [float(item.get("latency_ms", 0)) for item in composition_metrics if item.get("attempted")]
                ),
                "total_tokens": sum(int(item.get("total_tokens", 0)) for item in composition_metrics),
                "observational_quality": {
                    "attempted": _slice_metrics(attempted_cases),
                    "not_attempted": _slice_metrics(non_attempted_cases),
                },
            },
            "language_slices": {
                label: _slice_metrics(cases) for label, cases in sorted(language_slices.items())
            },
        }
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run the private development-only extraction benchmark."
    )
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    record = asyncio.run(run_live_development_evaluation())
    output = args.output
    if output is None:
        run_id = str(record["run_id"])
        output = DEFAULT_ARTIFACT_ROOT / run_id / "live-development.json"
    write_run_record(record, output)
    print(json.dumps({"output": str(output), "summary": record["summary"]}, indent=2))


if __name__ == "__main__":
    main()
