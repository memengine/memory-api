from __future__ import annotations

from types import SimpleNamespace

from api.tasks import extraction_tasks


def test_shadow_observation_is_bounded_and_never_returns_candidates() -> None:
    class ShadowExtractor:
        def extract_sync(self, **_kwargs):
            return SimpleNamespace(
                memories_to_store=[SimpleNamespace(content="must never escape")],
                pending_candidates=[SimpleNamespace(content="also private")],
                nothing_to_extract=False,
                tokens_used=19,
                provider_used="test-provider",
                extraction_metadata={
                    "proposal_confirmation": {
                        "accepted": 1,
                        "pending": 0,
                        "rejected_reasons": {"proposal_not_active": 2},
                    }
                },
            )

    observation = extraction_tasks._phase3a_shadow_observation(
        ShadowExtractor(),
        messages=[{"role": "user", "content": "Yes, the second one."}],
        proxy_user_id="proxy-1",
        tenant_id="tenant-1",
        job_id="job-1",
        existing_memories=[],
        source_context=None,
        proposal_context=[{"id": "proposal-1"}],
    )

    assert observation["status"] == "completed"
    assert observation["outcome"] == "accepted"
    assert observation["accepted_candidate_count"] == 1
    assert observation["write_blocked"] is True
    assert observation["tokens_used"] == 19
    assert "must never escape" not in str(observation)
    assert "also private" not in str(observation)
    assert "memories_to_store" not in observation
    assert "pending_candidates" not in observation


def test_shadow_observation_fails_open_without_error_content() -> None:
    class FailingShadowExtractor:
        def extract_sync(self, **_kwargs):
            raise RuntimeError("sensitive provider response")

    observation = extraction_tasks._phase3a_shadow_observation(
        FailingShadowExtractor(),
        messages=[],
        proxy_user_id="proxy-1",
        tenant_id="tenant-1",
        job_id="job-1",
        existing_memories=[],
        source_context=None,
        proposal_context=[{"id": "proposal-1"}],
    )

    assert observation["status"] == "error"
    assert observation["error_type"] == "RuntimeError"
    assert observation["write_blocked"] is True
    assert "sensitive provider response" not in str(observation)


def test_shadow_observation_bounds_untrusted_aggregate_fields() -> None:
    rejected = {f"proposal_reason_{index:02d}": index for index in range(20)}
    rejected["not_a_proposal_reason"] = 999

    class ShadowExtractor:
        def extract_sync(self, **_kwargs):
            return SimpleNamespace(
                memories_to_store=[],
                pending_candidates=[],
                nothing_to_extract=False,
                tokens_used=-5,
                provider_used="x" * 200,
                extraction_metadata={
                    "proposal_confirmation": {
                        "accepted": 0,
                        "pending": 0,
                        "rejected_reasons": rejected,
                    }
                },
            )

    observation = extraction_tasks._phase3a_shadow_observation(
        ShadowExtractor(),
        messages=[],
        proxy_user_id="proxy-1",
        tenant_id="tenant-1",
        job_id="job-1",
        existing_memories=[],
        source_context=None,
        proposal_context=[{"id": "proposal-1"}],
    )

    assert len(observation["rejected_reasons"]) <= 12
    assert all(key.startswith("proposal_") for key in observation["rejected_reasons"])
    assert len(observation["provider_used"]) == 64
    assert observation["tokens_used"] == 0
