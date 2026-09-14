from __future__ import annotations

import hashlib
import logging
import re
from time import perf_counter
from typing import Any

from api.services.extraction_service import ExtractionService as StructuredExtractionService


LOGGER = logging.getLogger("memoryos.universal_extraction_shadow")


class UniversalExtractionShadowService:
    """Observe the structured extractor beside Universal's legacy writer.

    This service is intentionally read-only. Its output is a compact comparison
    record: it contains hashes and aggregate governance signals, never customer
    message text or memory content. The Universal task keeps the legacy result
    as its sole write input until a separately approved migration gate passes.
    """

    def __init__(self, extractor: Any | None = None) -> None:
        self._extractor = extractor or StructuredExtractionService()

    def observe(
        self,
        *,
        messages: list[dict[str, Any]],
        legacy_memories: list[Any],
        user_uui_id: str,
        job_id: str | None,
    ) -> dict[str, Any]:
        started = perf_counter()
        try:
            result = self._extractor.extract_sync(
                messages=messages,
                proxy_user_id=user_uui_id,
                user_id=user_uui_id,
                job_id=job_id,
            )
            modern_memories = [*result.memories_to_store, *result.pending_candidates]
            comparison = self._comparison(legacy_memories, modern_memories)
            record = {
                "schema_version": 1,
                "status": "completed",
                "active_write_path_unchanged": True,
                "comparison": comparison,
                "modern_metrics": {
                    "tokens_used": int(result.tokens_used or 0),
                    "provider": result.provider_used,
                    "primary_pass": dict(result.extraction_metadata.get("primary_pass") or {}),
                    "compositional_pass": dict(
                        result.extraction_metadata.get("compositional_pass_metrics") or {}
                    ),
                },
                "observer_latency_ms": round((perf_counter() - started) * 1000, 3),
            }
            LOGGER.info(
                "universal_extraction_shadow_comparison",
                extra={"event": "universal_extraction_shadow_comparison", "job_id": job_id, **record},
            )
            return record
        except Exception as exc:  # Shadow observation must never block a permitted write.
            record = {
                "schema_version": 1,
                "status": "failed",
                "active_write_path_unchanged": True,
                "error_type": exc.__class__.__name__,
                "observer_latency_ms": round((perf_counter() - started) * 1000, 3),
            }
            LOGGER.warning(
                "universal_extraction_shadow_failed",
                extra={"event": "universal_extraction_shadow_failed", "job_id": job_id, **record},
            )
            return record

    @classmethod
    def _comparison(cls, legacy_memories: list[Any], modern_memories: list[Any]) -> dict[str, Any]:
        legacy = cls._summaries(legacy_memories)
        modern = cls._summaries(modern_memories)
        shared_propositions = set(legacy["by_proposition"]) & set(modern["by_proposition"])
        category_agreement = sum(
            legacy["by_proposition"][fingerprint] == modern["by_proposition"][fingerprint]
            for fingerprint in shared_propositions
        )
        modern_evidence = [
            dict(getattr(memory, "validated_evidence", {}) or {})
            for memory in modern_memories
        ]
        return {
            "legacy_memory_count": len(legacy_memories),
            "modern_candidate_count": len(modern_memories),
            "proposition_overlap_count": len(shared_propositions),
            "category_agreement_count": category_agreement,
            "legacy_category_counts": legacy["category_counts"],
            "modern_category_counts": modern["category_counts"],
            "modern_validated_evidence_count": sum(bool(item) for item in modern_evidence),
            "modern_authority_levels": sorted(
                {
                    int(dict(item.get("authority") or {}).get("level", 0))
                    for item in modern_evidence
                    if item
                }
            ),
            # Conflict resolution intentionally does not run in shadow mode: a
            # shadow observer may never query or mutate the active write state.
            "conflict_resolution": "not_run_read_only_shadow",
            "proposition_fingerprints": sorted(shared_propositions),
        }

    @classmethod
    def _summaries(cls, memories: list[Any]) -> dict[str, Any]:
        by_proposition: dict[str, str] = {}
        category_counts: dict[str, int] = {}
        for memory in memories:
            content = str(getattr(memory, "content", "") or "")
            category = str(getattr(memory, "category", "unknown") or "unknown")
            category = getattr(getattr(memory, "category", None), "value", category)
            fingerprint = cls._fingerprint(content)
            if not fingerprint:
                continue
            by_proposition[fingerprint] = str(category)
            category_counts[str(category)] = category_counts.get(str(category), 0) + 1
        return {"by_proposition": by_proposition, "category_counts": category_counts}

    @staticmethod
    def _fingerprint(content: str) -> str:
        normalized = " ".join(re.findall(r"[a-z0-9]+", content.lower()))
        if not normalized:
            return ""
        return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16]


__all__ = ["UniversalExtractionShadowService"]
