from __future__ import annotations

from types import SimpleNamespace

from api.schemas.memory_schemas import ExtractedMemory
from api.services.universal_extraction_shadow_service import UniversalExtractionShadowService


class FakeStructuredExtractor:
    def extract_sync(self, **_kwargs):
        return SimpleNamespace(
            memories_to_store=[
                ExtractedMemory(
                    content="User prefers concise explanations.",
                    category="preference",
                    importance_score=7.0,
                    confidence=0.92,
                    expiry="permanent",
                    reasoning="Direct statement",
                    validated_evidence={"authority": {"level": 20}},
                )
            ],
            pending_candidates=[],
            tokens_used=23,
            provider_used="test",
            extraction_metadata={
                "primary_pass": {"total_tokens": 23},
                "compositional_pass_metrics": {"attempted": False},
            },
        )


def test_shadow_compares_hashed_propositions_and_never_returns_content() -> None:
    service = UniversalExtractionShadowService(extractor=FakeStructuredExtractor())
    legacy = [
        ExtractedMemory(
            content="User prefers concise explanations.",
            category="preference",
            importance_score=7.0,
            confidence=0.92,
            expiry="permanent",
            reasoning="Legacy extraction",
        )
    ]

    record = service.observe(
        messages=[{"role": "user", "content": "Keep explanations concise."}],
        legacy_memories=legacy,
        user_uui_id="user-1",
        job_id="job-1",
    )

    assert record["status"] == "completed"
    assert record["active_write_path_unchanged"] is True
    assert record["comparison"]["proposition_overlap_count"] == 1
    assert record["comparison"]["category_agreement_count"] == 1
    assert record["comparison"]["modern_validated_evidence_count"] == 1
    assert record["comparison"]["modern_authority_levels"] == [20]
    assert record["comparison"]["conflict_resolution"] == "not_run_read_only_shadow"
    assert "concise explanations" not in str(record)


def test_shadow_failure_is_fail_open() -> None:
    class FailingExtractor:
        def extract_sync(self, **_kwargs):
            raise RuntimeError("provider unavailable")

    record = UniversalExtractionShadowService(extractor=FailingExtractor()).observe(
        messages=[{"role": "user", "content": "Hello"}],
        legacy_memories=[],
        user_uui_id="user-1",
        job_id="job-1",
    )

    assert record["status"] == "failed"
    assert record["active_write_path_unchanged"] is True
    assert record["error_type"] == "RuntimeError"
