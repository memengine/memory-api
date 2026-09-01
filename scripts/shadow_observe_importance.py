from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import time
from collections import Counter, defaultdict
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from api.services.extraction_service import ExtractionService
from api.services.importance_shadow_service import ImportanceShadowService
from api.services.llm_service import LLMResponse, LLMService
from api.settings import get_settings

RATES = {("openai", "gpt-4o-mini"): (0.15, 0.60)}


class NoopUsageCache:
    async def increment_provider_usage(self, provider: str, hour_bucket: str, ttl: int) -> None:
        del provider, hour_bucket, ttl


class RecordingLLM:
    def __init__(self) -> None:
        self.service = LLMService()
        self.responses: list[LLMResponse] = []

    async def complete(self, **kwargs: Any) -> LLMResponse:
        response = await self.service.complete(**kwargs)
        self.responses.append(response)
        return response


class CollectingObserver(ImportanceShadowService):
    def __init__(self) -> None:
        super().__init__()
        self.latest = ()
        self.latest_latency_ms: float | None = None
        self.failures = 0

    def observe(self, **kwargs: Any):
        started = time.perf_counter()
        try:
            self.latest = super().observe(**kwargs)
            self.latest_latency_ms = round((time.perf_counter() - started) * 1000, 3)
            return self.latest
        except Exception:
            self.latest_latency_ms = round((time.perf_counter() - started) * 1000, 3)
            self.failures += 1
            raise


def workflows(count: int) -> list[dict[str, Any]]:
    roles = ["operations analyst", "mobile engineer", "customer success lead", "research coordinator", "finance controller", "product designer", "security engineer", "sales planner", "data engineer", "support manager"]
    companies = ["Harbor Labs", "Juniper Works", "Blue Mesa", "Cedar Cloud", "Atlas Forge", "Riverline", "Quartz Health", "Northwind Studio", "Beacon Retail", "Maple Systems"]
    skills = ["Kotlin", "Terraform", "Tableau", "PostgreSQL", "Figma", "Kubernetes", "R", "Salesforce", "Go", "Power BI"]
    preferences = ["bullet-point status updates", "examples before definitions", "24-hour times", "neutral professional wording", "tables for comparisons", "brief morning summaries", "metric units", "step-by-step troubleshooting", "plain-language explanations", "weekly digest emails"]
    goals = ["reduce support handoff time", "complete a cloud certification", "launch a customer portal", "improve forecast accuracy", "automate monthly reporting", "move into technical leadership", "cut deployment rollback time", "publish a research report", "standardize design reviews", "improve account retention"]
    procedures = ["reviews alerts every morning", "archives signed reports each month", "runs a checklist before releases", "blocks Tuesday afternoons for planning", "reconciles usage data every Thursday", "reviews customer feedback each Friday", "pairs on risky migrations", "writes a decision note after architecture reviews", "checks accessibility before design handoff", "backs up analysis files weekly"]
    cities = ["Jaipur", "Mysuru", "Indore", "Surat", "Nagpur", "Coimbatore", "Lucknow", "Bhopal", "Vadodara", "Kolkata"]
    managers = ["Aarav", "Meera", "Kabir", "Isha", "Rohan", "Anika", "Dev", "Tara", "Vikram", "Naina"]
    items = []
    for index in range(count):
        slot = index % 10
        uncertain = index % 5 == 0
        preference = (
            f"I might prefer {preferences[slot]}, but I am not certain yet."
            if uncertain
            else f"I consistently prefer {preferences[slot]}."
        )
        if index % 3 == 0:
            text = (
                f"I work as an {roles[slot]} at {companies[slot]}. "
                f"I use {skills[slot]} regularly. {preference} "
                f"My current goal is to {goals[slot]}. I {procedures[slot]}."
            )
        elif index % 3 == 1:
            text = (
                f"I live in {cities[slot]}, and {managers[slot]} is my manager. "
                f"I am proficient with {skills[slot]}. {preference} "
                f"This quarter I plan to {goals[slot]}."
            )
        else:
            text = (
                f"At {companies[slot]}, I {procedures[slot]}. {managers[slot]} is the stakeholder I report to. "
                f"My main work area uses {skills[slot]}. {preference} "
                f"I am actively trying to {goals[slot]}."
            )
        items.append({"id": f"dev-shadow-workflow-{index + 1:03d}", "messages": [{"role": "user", "content": text}]})
    return items


def call_record(response: LLMResponse) -> dict[str, Any]:
    return {"provider": response.provider_used, "model": response.model_used, "input_tokens": response.input_tokens, "output_tokens": response.output_tokens, "total_tokens": response.total_tokens, "latency_ms": response.latency_ms}


def estimated_cost(calls: list[dict[str, Any]]) -> float:
    total = 0.0
    for call in calls:
        rate = RATES.get((str(call["provider"]).lower(), str(call["model"])))
        if rate:
            total += call["input_tokens"] / 1_000_000 * rate[0] + call["output_tokens"] / 1_000_000 * rate[1]
    return total


def summarize(rows: list[dict[str, Any]], observer_failures: int) -> dict[str, Any]:
    comparisons = [item for row in rows for item in row.get("comparisons", [])]
    deltas = [float(item["delta"]) for item in comparisons]
    latencies = [float(row["observer_latency_ms"]) for row in rows if row.get("observer_latency_ms") is not None]
    by_category: dict[str, list[dict[str, Any]]] = defaultdict(list)
    by_disposition: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for item in comparisons:
        by_category[item["category"]].append(item)
        by_disposition[item["disposition"]].append(item)

    def group(items: list[dict[str, Any]]) -> dict[str, Any]:
        values = [float(item["delta"]) for item in items]
        return {"count": len(items), "agreement_rate": sum(value == 0 for value in values) / len(values) if values else 1.0, "mean_absolute_delta": sum(abs(value) for value in values) / len(values) if values else 0.0, "shadow_below_model": sum(value < 0 for value in values), "shadow_above_model": sum(value > 0 for value in values)}

    calls = [call for row in rows for call in row.get("provider_calls", [])]
    return {
        "workflow_count": len(rows),
        "completed_workflows": sum(row.get("status") == "completed" for row in rows),
        "errored_workflows": sum(row.get("status") == "error" for row in rows),
        "memory_count": len(comparisons),
        "agreement_rate": sum(delta == 0 for delta in deltas) / len(deltas) if deltas else 1.0,
        "mean_absolute_score_delta": sum(abs(delta) for delta in deltas) / len(deltas) if deltas else 0.0,
        "shadow_below_model": sum(delta < 0 for delta in deltas),
        "shadow_above_model": sum(delta > 0 for delta in deltas),
        "by_category": {key: group(value) for key, value in sorted(by_category.items())},
        "by_disposition": {key: group(value) for key, value in sorted(by_disposition.items())},
        "model_score_distribution": dict(sorted(Counter(str(item["model_score"]) for item in comparisons).items())),
        "shadow_score_distribution": dict(sorted(Counter(str(item["shadow_score"]) for item in comparisons).items())),
        "observer_latency_ms": {"mean": statistics.fmean(latencies) if latencies else 0.0, "p50": statistics.median(latencies) if latencies else 0.0, "max": max(latencies) if latencies else 0.0},
        "observer_failures": observer_failures,
        "active_model_scores_unchanged": all(row.get("active_model_scores_unchanged", False) for row in rows),
        "provider_calls": len(calls),
        "token_usage": {"input": sum(call["input_tokens"] for call in calls), "output": sum(call["output_tokens"] for call in calls), "total": sum(call["total_tokens"] for call in calls)},
        "estimated_provider_cost_usd": estimated_cost(calls),
    }


async def run(output: Path, maximum_workflows: int, minimum_memories: int) -> dict[str, Any]:
    settings = get_settings()
    if settings.app_env.strip().lower() != "development" or not settings.importance_shadow_enabled:
        raise RuntimeError("Set APP_ENV=development and IMPORTANCE_SHADOW_ENABLED=true")
    recorder = RecordingLLM()
    observer = CollectingObserver()
    extraction = ExtractionService(llm_service=recorder, cache_service=NoopUsageCache(), importance_shadow_service=observer)
    rows: list[dict[str, Any]] = []
    for workflow in workflows(maximum_workflows):
        recorder.responses.clear()
        observer.latest = ()
        observer.latest_latency_ms = None
        try:
            result = await extraction.extract(messages=workflow["messages"], proxy_user_id=workflow["id"], tenant_id="development-shadow-observation", job_id=workflow["id"])
            comparisons = [asdict(item) for item in observer.latest]
            active_scores = [float(item.importance_score) for item in result.memories_to_store] + [float(item.importance_score) for item in result.pending_candidates]
            rows.append({"id": workflow["id"], "status": "completed", "error_type": None, "comparisons": comparisons, "observer_latency_ms": observer.latest_latency_ms, "active_model_scores_unchanged": active_scores == [float(item["model_score"]) for item in comparisons], "provider_calls": [call_record(response) for response in recorder.responses]})
        except Exception as exc:
            rows.append({"id": workflow["id"], "status": "error", "error_type": exc.__class__.__name__, "comparisons": [], "observer_latency_ms": None, "active_model_scores_unchanged": True, "provider_calls": [call_record(response) for response in recorder.responses]})
        record = {"schema_version": "1.0", "mode": "development-importance-shadow-observation", "created_at": datetime.now(UTC).isoformat(), "holdout_loaded": False, "benchmark_cases_used": False, "scorer_tuned_during_window": False, "active_scores_applied": False, "config": {"app_env": settings.app_env, "provider_order": settings.llm_provider_order, "minimum_memories": minimum_memories, "maximum_workflows": maximum_workflows}, "summary": summarize(rows, observer.failures), "workflows": rows}
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        categories = set(record["summary"]["by_category"])
        dispositions = set(record["summary"]["by_disposition"])
        if record["summary"]["memory_count"] >= minimum_memories and len(categories) >= 6 and {"store", "pending"} <= dispositions:
            break
    return record


def main() -> None:
    parser = argparse.ArgumentParser(description="Development-only deterministic importance shadow observation.")
    parser.add_argument("--output", type=Path, default=Path("artifacts/internal-benchmarks/importance-shadow-observation-development.json"))
    parser.add_argument("--maximum-workflows", type=int, default=60)
    parser.add_argument("--minimum-memories", type=int, default=200)
    args = parser.parse_args()
    result = asyncio.run(run(args.output, args.maximum_workflows, args.minimum_memories))
    print(json.dumps(result["summary"], indent=2))


if __name__ == "__main__":
    main()
