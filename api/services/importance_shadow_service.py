from __future__ import annotations

import logging
import json
import re
import sys
import uuid
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from time import perf_counter
from typing import Any, Iterable

LOGGER = logging.getLogger("memoryos.importance_shadow")


@dataclass(frozen=True, slots=True)
class ImportanceShadowComparison:
    memory_index: int
    category: str
    disposition: str
    model_score: float
    shadow_score: float
    delta: float
    features: dict[str, Any]


class ImportanceShadowService:
    """Development-only observer; never mutates extraction results."""

    def __init__(self, scorer: Any | None = None, review_dir: str | Path | None = None) -> None:
        if scorer is None:
            # The accepted scorer stays owned by the internal benchmark until a
            # separate activation decision is approved.
            repository_root = str(Path(__file__).resolve().parents[2])
            if repository_root not in sys.path:
                sys.path.insert(0, repository_root)
            from benchmarks.internal.deterministic_importance import DeterministicImportanceScorer

            scorer = DeterministicImportanceScorer()
        self._scorer = scorer
        self._review_dir = Path(review_dir) if review_dir else None
        self.latest_latency_ms: float | None = None

    def observe(
        self,
        *,
        stored: Iterable[Any],
        pending: Iterable[Any],
        messages: list[dict[str, Any]],
        tenant_id: str | None,
        proxy_user_id: str,
        job_id: str | None,
    ) -> tuple[ImportanceShadowComparison, ...]:
        started = perf_counter()
        comparisons: list[ImportanceShadowComparison] = []
        candidates = [(memory, "store") for memory in stored]
        candidates.extend((memory, "pending") for memory in pending)
        active_scores_before = [
            float(self._memory_payload(memory, disposition)["importance_score"])
            for memory, disposition in candidates
        ]
        for index, (memory, disposition) in enumerate(candidates):
            payload = self._memory_payload(memory, disposition)
            result = self._scorer.score(payload, messages)
            model_score = float(payload["importance_score"])
            comparisons.append(
                ImportanceShadowComparison(
                    memory_index=index,
                    category=str(payload["category"]),
                    disposition=disposition,
                    model_score=model_score,
                    shadow_score=float(result.score),
                    delta=float(result.score) - model_score,
                    features=asdict(result.features),
                )
            )

        elapsed_ms = round((perf_counter() - started) * 1000, 3)
        self.latest_latency_ms = elapsed_ms
        active_scores_after = [
            float(self._memory_payload(memory, disposition)["importance_score"])
            for memory, disposition in candidates
        ]
        active_scores_unchanged = active_scores_before == active_scores_after
        LOGGER.info(
            "importance_shadow_comparison",
            extra={
                "event": "importance_shadow_comparison",
                "tenant_id": tenant_id,
                "proxy_user_id": proxy_user_id,
                "job_id": job_id,
                "memory_count": len(comparisons),
                "changed_count": sum(item.delta != 0 for item in comparisons),
                "mean_absolute_delta": (
                    sum(abs(item.delta) for item in comparisons) / len(comparisons)
                    if comparisons
                    else 0.0
                ),
                "latency_ms": elapsed_ms,
                "provider_calls": 0,
                "fallback_count": 0,
                "active_scores_unchanged": active_scores_unchanged,
                "comparisons": [asdict(item) for item in comparisons],
            },
        )
        self._write_review_capture(
            comparisons=comparisons,
            candidates=candidates,
            messages=messages,
            tenant_id=tenant_id,
            proxy_user_id=proxy_user_id,
            job_id=job_id,
            latency_ms=elapsed_ms,
            active_scores_unchanged=active_scores_unchanged,
        )
        self._write_telemetry_event({
            "status": "success",
            "tenant_id": tenant_id,
            "proxy_user_id": proxy_user_id,
            "job_id": job_id,
            "memory_count": len(comparisons),
            "changed_count": sum(item.delta != 0 for item in comparisons),
            "latency_ms": elapsed_ms,
            "provider_calls": 0,
            "fallback_count": 0,
            "active_scores_unchanged": active_scores_unchanged,
        })
        return tuple(comparisons)

    def record_failure(
        self,
        *,
        error: Exception,
        tenant_id: str | None,
        proxy_user_id: str,
        job_id: str | None,
    ) -> None:
        """Persist observer failure telemetry without affecting extraction."""
        self._write_telemetry_event({
            "status": "failure",
            "tenant_id": tenant_id,
            "proxy_user_id": proxy_user_id,
            "job_id": job_id,
            "memory_count": 0,
            "provider_calls": 0,
            "fallback_count": 1,
            "active_scores_unchanged": True,
            "error_type": error.__class__.__name__,
            "error": str(error),
        })

    def _write_review_capture(
        self,
        *,
        comparisons: list[ImportanceShadowComparison],
        candidates: list[tuple[Any, str]],
        messages: list[dict[str, Any]],
        tenant_id: str | None,
        proxy_user_id: str,
        job_id: str | None,
        latency_ms: float,
        active_scores_unchanged: bool,
    ) -> None:
        if self._review_dir is None or not comparisons:
            return
        self._review_dir.mkdir(parents=True, exist_ok=True)
        safe_job = re.sub(r"[^A-Za-z0-9_.-]", "-", str(job_id or "unassigned"))[:80]
        target = self._review_dir / f"{safe_job}-{uuid.uuid4().hex}.json"
        temporary = target.with_suffix(".tmp")
        memories = []
        for comparison, (memory, _) in zip(comparisons, candidates):
            payload = self._memory_payload(memory, comparison.disposition)
            memories.append({
                "memory_index": comparison.memory_index,
                "content": str(payload.get("content") or ""),
                "category": comparison.category,
                "disposition": comparison.disposition,
                "model_score": comparison.model_score,
                "deterministic_score": comparison.shadow_score,
                "delta": comparison.delta,
            })
        capture = {
            "schema_version": "1.0",
            "captured_at": datetime.now(UTC).isoformat(),
            "tenant_id": tenant_id,
            "proxy_user_id": proxy_user_id,
            "job_id": job_id,
            "telemetry": {
                "observer_latency_ms": latency_ms,
                "observer_status": "success",
                "provider_calls": 0,
                "fallback_count": 0,
                "active_scores_unchanged": active_scores_unchanged,
            },
            "messages": messages,
            "memories": memories,
        }
        temporary.write_text(json.dumps(capture, indent=2) + "\n", encoding="utf-8")
        temporary.replace(target)

    def _write_telemetry_event(self, event: dict[str, Any]) -> None:
        if self._review_dir is None:
            return
        telemetry_dir = self._review_dir / "_telemetry"
        telemetry_dir.mkdir(parents=True, exist_ok=True)
        event_id = uuid.uuid4().hex
        target = telemetry_dir / f"{event_id}.json"
        temporary = target.with_suffix(".tmp")
        payload = {
            "schema_version": "1.0",
            "event_id": event_id,
            "recorded_at": datetime.now(UTC).isoformat(),
            **event,
        }
        temporary.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        temporary.replace(target)

    @staticmethod
    def _memory_payload(memory: Any, disposition: str) -> dict[str, Any]:
        if hasattr(memory, "model_dump"):
            payload = memory.model_dump()
        elif hasattr(memory, "__dict__"):
            payload = vars(memory).copy()
        else:
            payload = {
                "content": getattr(memory, "content"),
                "category": getattr(memory, "category"),
                "importance_score": getattr(memory, "importance_score"),
            }
        payload["disposition"] = disposition
        return payload


__all__ = ["ImportanceShadowComparison", "ImportanceShadowService"]
