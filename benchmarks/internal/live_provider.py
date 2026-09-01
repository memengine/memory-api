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
from api.services.evidence_attribution_service import EvidenceAttributionService
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
    cases = load_development_cases()
    recorder = RecordingLLMService(LLMService())
    extraction = ExtractionService(
        llm_service=recorder,
        cache_service=NoopUsageCache(),
    )
    attributor = EvidenceAttributionService(recorder)
    metrics = []
    details: list[dict[str, Any]] = []

    for case in cases:
        recorder.responses.clear()
        started = time.perf_counter()
        predictions: list[dict[str, Any]] = []
        error: dict[str, Any] | None = None
        attribution_error: dict[str, Any] | None = None
        extraction_call_count = 0
        extraction_output_unchanged = True
        try:
            result = await extraction.extract(
                messages=list(case.messages),
                proxy_user_id=f"internal-benchmark-{case.id}",
                tenant_id="internal-benchmark-development",
                job_id=f"internal-benchmark-{case.id}",
            )
            predictions.extend(
                {
                    "content": item.content,
                    "category": str(item.category),
                    "disposition": "store",
                    "importance_score": float(item.importance_score),
                    "confidence": float(item.confidence),
                    "reasoning": item.reasoning,
                    "evidence_turns": [],
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
                    "evidence_turns": [],
                }
                for item in result.pending_candidates
            )
            extraction_call_count = len(recorder.responses)
            extraction_snapshot = [
                {key: value for key, value in prediction.items() if key != "evidence_turns"}
                for prediction in predictions
            ]
            try:
                attribution = await attributor.attribute(
                    memories=predictions,
                    messages=list(case.messages),
                )
                for memory_index, evidence_turns in attribution.evidence_by_memory.items():
                    predictions[memory_index]["evidence_turns"] = evidence_turns
            except (ProviderError, AllProvidersFailedError) as exc:
                attribution_error = _error_record("attribution_provider_error", exc)
            except Exception as exc:
                attribution_error = _error_record("attribution_harness_error", exc)
            extraction_output_unchanged = extraction_snapshot == [
                {key: value for key, value in prediction.items() if key != "evidence_turns"}
                for prediction in predictions
            ]
        except (ProviderError, AllProvidersFailedError) as exc:
            error = _error_record("provider_error", exc)
        except ExtractionError as exc:
            error = _error_record("model_output_error", exc)
        except Exception as exc:
            error = _error_record("benchmark_harness_error", exc)

        latency_ms = (time.perf_counter() - started) * 1000
        calls = [_response_record(response) for response in recorder.responses]
        extraction_calls = calls[:extraction_call_count]
        attribution_calls = calls[extraction_call_count:]
        estimated_cost, pricing_warnings = _estimate_cost(recorder.responses)
        attribution_cost, attribution_pricing_warnings = _estimate_cost(
            recorder.responses[extraction_call_count:]
        )
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
                "attribution_error": attribution_error,
                "extraction_output_unchanged": extraction_output_unchanged,
                "latency_ms": round(latency_ms, 3),
                "provider_calls": calls,
                "extraction_provider_calls": extraction_calls,
                "attribution_provider_calls": attribution_calls,
                "attribution_cost_usd": attribution_cost,
                "attribution_pricing_warnings": attribution_pricing_warnings,
                "pricing_warnings": pricing_warnings,
                "predictions": predictions,
                "metrics": asdict(case_metrics),
            }
        )

    record = build_run_record(
        cases,
        metrics,
        config={
            "mode": "live-provider-development-only",
            "holdout_loaded": False,
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


def _add_live_summary(record: dict[str, Any], details: list[dict[str, Any]]) -> None:
    calls = [call for case in details for call in case["provider_calls"]]
    latencies = [float(case["latency_ms"]) for case in details]
    attribution_calls = [
        call for case in details for call in case.get("attribution_provider_calls", [])
    ]
    attribution_errors = Counter(
        case["attribution_error"]["kind"]
        for case in details
        if case.get("attribution_error") is not None
    )
    errors = Counter(
        case["error"]["kind"]
        for case in details
        if case["error"] is not None
    )
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
            "extraction_output_unchanged": all(
                case.get("extraction_output_unchanged", False) for case in details
            ),
            "attribution": {
                "call_count": len(attribution_calls),
                "errors_by_kind": dict(attribution_errors),
                "latency_ms_total": sum(call["latency_ms"] for call in attribution_calls),
                "latency_ms_mean_per_call": (
                    sum(call["latency_ms"] for call in attribution_calls) / len(attribution_calls)
                    if attribution_calls else 0.0
                ),
                "token_usage": {
                    "input": sum(call["input_tokens"] for call in attribution_calls),
                    "output": sum(call["output_tokens"] for call in attribution_calls),
                    "total": sum(call["total_tokens"] for call in attribution_calls),
                },
                "estimated_cost_usd": sum(
                    float(case.get("attribution_cost_usd", 0.0)) for case in details
                ),
                "pricing_warning_count": sum(
                    len(case.get("attribution_pricing_warnings", [])) for case in details
                ),
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
