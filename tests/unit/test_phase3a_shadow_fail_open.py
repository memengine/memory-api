from __future__ import annotations

from api.tasks import extraction_tasks


def test_shadow_gate_failure_cannot_fail_normal_pipeline(monkeypatch) -> None:
    def fail_context_lookup(*_args, **_kwargs):
        raise RuntimeError("database details must not escape")

    monkeypatch.setattr(
        extraction_tasks,
        "_active_proposal_context",
        fail_context_lookup,
    )

    observation = extraction_tasks._run_phase3a_shadow_observation(
        object(),
        job_payload={"job_id": "job-1"},
        messages=[{"role": "user", "content": "yes"}],
        proxy_user_id="proxy-1",
        tenant_id="tenant-1",
        existing_memories=[],
        source_context=None,
        client=None,
    )

    assert observation["status"] == "error"
    assert observation["error_type"] == "RuntimeError"
    assert observation["write_blocked"] is True
    assert "database details must not escape" not in str(observation)
