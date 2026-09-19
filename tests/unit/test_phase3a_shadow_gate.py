from __future__ import annotations

from api.tasks import extraction_tasks


def test_shadow_gate_skips_provider_without_active_proposal(monkeypatch) -> None:
    monkeypatch.setattr(
        extraction_tasks,
        "_active_proposal_context",
        lambda *_args, **_kwargs: [],
    )

    def unexpected_factory(_client):
        raise AssertionError("shadow provider must not run without an active proposal")

    monkeypatch.setattr(
        extraction_tasks,
        "_build_phase3a_shadow_extractor",
        unexpected_factory,
    )

    observation = extraction_tasks._run_phase3a_shadow_observation(
        object(),
        job_payload={"job_id": "job-1"},
        messages=[{"role": "user", "content": "hello"}],
        proxy_user_id="proxy-1",
        tenant_id="tenant-1",
        existing_memories=[],
        source_context=None,
        client=None,
    )

    assert observation == {
        "enabled": True,
        "eligible": False,
        "attempted": False,
        "write_blocked": True,
        "active_proposal_count": 0,
        "status": "not_eligible",
    }


def test_shadow_gate_uses_message_copy_and_returns_only_observation(monkeypatch) -> None:
    original_messages = [
        {"role": "assistant", "content": "I can remember that."},
        {"role": "user", "content": "Yes, remember it."},
    ]
    shadow_extractor = object()
    captured: dict[str, object] = {}

    def active_proposals(_session, *, job_payload, messages):
        assert job_payload["job_id"] == "job-1"
        messages[0]["_registered_memory_proposal"] = True
        return [{"id": "proposal-1", "turn_index": 0}]

    def observe(extractor, **kwargs):
        captured["extractor"] = extractor
        captured.update(kwargs)
        return {
            "enabled": True,
            "eligible": True,
            "attempted": True,
            "write_blocked": True,
            "status": "completed",
            "outcome": "accepted",
            "tokens_used": 11,
        }

    monkeypatch.setattr(extraction_tasks, "_active_proposal_context", active_proposals)
    monkeypatch.setattr(
        extraction_tasks,
        "_build_phase3a_shadow_extractor",
        lambda _client: shadow_extractor,
    )
    monkeypatch.setattr(extraction_tasks, "_phase3a_shadow_observation", observe)

    observation = extraction_tasks._run_phase3a_shadow_observation(
        object(),
        job_payload={"job_id": "job-1"},
        messages=original_messages,
        proxy_user_id="proxy-1",
        tenant_id="tenant-1",
        existing_memories=[],
        source_context=None,
        client=None,
    )

    assert "_registered_memory_proposal" not in original_messages[0]
    assert captured["messages"][0]["_registered_memory_proposal"] is True
    assert captured["extractor"] is shadow_extractor
    assert observation["outcome"] == "accepted"
    assert "memories_to_store" not in observation
    assert "pending_candidates" not in observation
