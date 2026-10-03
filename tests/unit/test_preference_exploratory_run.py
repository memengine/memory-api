"""Offline executor safety tests. Mock responses are not semantic evaluations."""
import json
from copy import deepcopy
from decimal import Decimal
from pathlib import Path

import httpx
import pytest

from benchmarks.internal import preference_exploratory_run as runner


@pytest.fixture(scope="module")
def prepared():
    return runner.prepare_execution()


def subset(prepared, count=2, *, controls=False):
    original, jobs = prepared
    selected = [job for job in jobs if (job[2] is None) == controls][:count]
    plan = deepcopy(original)
    ids = {job[0] for job in selected}
    plan["cases"] = [row for row in plan["cases"] if row["case_id"] in ids]
    plan.pop("execution_sha256")
    plan["execution_sha256"] = runner.prep.digest(runner.prep.canonical(plan))
    return plan, selected


def response(**updates):
    result = {
        "model": "gpt-4.1-mini-2025-04-14", "service_tier": "default",
        "usage": {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120},
        "choices": [{"finish_reason": "stop", "message": {"role": "assistant", "refusal": None,
                    "content": '{"findings":[{"kind":"abstain","reason":"not_preference"}]}'}}],
    }
    result.update(updates)
    return result


def run_mock(monkeypatch, tmp_path, prepared, payload=None, count=2, **kwargs):
    plan, jobs = subset(prepared, count)
    calls = []

    def fetch(client, request, key):
        calls.append(request)
        if isinstance(payload, Exception):
            raise payload
        return deepcopy(payload or response())

    monkeypatch.setattr(runner, "fetch", fetch)
    summary = runner.execute(plan, jobs, approved_digest=plan["execution_sha256"],
                             client=None, api_key="test-secret", output_root=tmp_path, **kwargs)
    journal = tmp_path / f'run-{plan["execution_sha256"]}' / "journal.jsonl"
    events = [json.loads(line) for line in journal.read_text(encoding="utf-8").splitlines()]
    assert "test-secret" not in journal.read_text(encoding="utf-8")
    return summary, events, calls, plan, jobs


def test_prepare_binds_explicit_standard_tier_without_changing_base(prepared):
    plan, jobs = prepared
    assert plan == runner.prepare_execution()[0]
    assert plan["base_preflight_sha256"] == runner.APPROVED_BASE
    assert plan["request_amendment"] == {"service_tier": "default"}
    assert plan["cap_usd"] == "2.00"
    assert plan["per_attempt_reservation_usd"] == "0.43"
    assert sum(job[2] is not None for job in jobs) == 339
    assert sum(job[2] is None for job in jobs) == 21
    for key in ("holdouts_loaded", "ai_annotations_loaded", "semantic_accuracy_measured", "human_release_gate_met"):
        assert plan[key] is False
    base = runner.prep.build_preflight()
    for (_, _, request), original, amended in zip(jobs, base["cases"], plan["cases"], strict=True):
        if request is not None:
            assert request["service_tier"] == "default"
            assert amended["request_sha256"] != original["request_sha256"]
            unamended = {key: value for key, value in request.items() if key != "service_tier"}
            assert runner.prep.digest(runner.prep.canonical(unamended)) == original["request_sha256"]


def test_preparation_reads_only_fixed_public_assets(monkeypatch):
    allowed = {
        runner.prep.PROTOCOL, runner.prep.INSTRUCTIONS, runner.prep.FREEZE,
        runner.prep.ROOT / "benchmarks/internal/datasets/preference_semantic/development/inputs-v1.json",
        runner.prep.ROOT / "benchmarks/internal/datasets/preference_semantic/development/manifest-v1.json",
        runner.prep.ROOT / "benchmarks/internal/preference_exploratory.py",
        runner.prep.ROOT / "benchmarks/internal/preference_exploratory_run.py",
        *(runner.prep.ROOT / path for path in runner.prep.FROZEN_PATHS),
    }
    original = Path.read_text

    def read(path, *args, **kwargs):
        assert path in allowed, f"unexpected read: {path}"
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", read)
    runner.prepare_execution()


def test_changed_base_preflight_cannot_create_execution_plan(monkeypatch):
    monkeypatch.setattr(runner.prep, "build_preflight", lambda: {"preflight_sha256": "changed"})
    with pytest.raises(ValueError, match="approved base preflight changed"):
        runner.prepare_execution()


def test_reservation_is_durable_before_dispatch_and_first_smoke_is_not_repeated(monkeypatch, tmp_path, prepared):
    plan, jobs = subset(prepared)
    calls = []

    def fetch(client, request, key):
        path = tmp_path / f'run-{plan["execution_sha256"]}' / "journal.jsonl"
        event = json.loads(path.read_text(encoding="utf-8").splitlines()[-1])
        assert event["event"] == "reserved_before_dispatch"
        assert event["reserved_usd"] == "0.43"
        calls.append(request)
        return response()

    monkeypatch.setattr(runner, "fetch", fetch)
    summary = runner.execute(plan, jobs, approved_digest=plan["execution_sha256"],
                             client=None, api_key="test", output_root=tmp_path)
    assert summary["status"] == "complete"
    assert summary["provider_attempts"] == summary["accepted_offline_proposals"] == len(calls) == 2
    assert summary["uncached_token_cost_upper_bound_usd"] == "0.000144"
    assert summary["uncertain_charge_reservation_usd"] == "0"
    assert (tmp_path / f'run-{plan["execution_sha256"]}' / "summary.json").exists()
    with pytest.raises(FileExistsError):
        runner.execute(plan, jobs, approved_digest=plan["execution_sha256"],
                       client=None, api_key="test", output_root=tmp_path)
    assert len(calls) == 2


@pytest.mark.parametrize("change", ["approval", "request", "budget", "coverage", "credential"])
def test_no_dispatch_on_unapproved_or_changed_execution(monkeypatch, tmp_path, prepared, change):
    plan, jobs = deepcopy(subset(prepared))
    approval, key = plan["execution_sha256"], "test"
    if change == "approval":
        approval = "f" * 64
    elif change == "request":
        jobs[0][2]["messages"][0]["content"] = "changed"
    elif change == "budget":
        plan["cap_usd"] = "3"
    elif change == "coverage":
        jobs = jobs[:-1]
    else:
        key = ""
    monkeypatch.setattr(runner, "fetch", lambda *args: pytest.fail("must not dispatch"))
    with pytest.raises(ValueError):
        runner.execute(plan, jobs, approved_digest=approval, client=None, api_key=key, output_root=tmp_path)
    assert not list(tmp_path.iterdir())


def test_budget_uses_max_context_reserve_not_request_byte_estimate(monkeypatch, tmp_path, prepared):
    payload = response(usage={"prompt_tokens": 1_000_000, "completion_tokens": 0, "total_tokens": 1_000_000})
    summary, _, calls, _, _ = run_mock(monkeypatch, tmp_path, prepared, payload, count=6)
    assert len(calls) == 4
    assert summary["stop_reason"] == "budget_reservation_unavailable"
    assert Decimal(summary["uncached_token_cost_upper_bound_usd"]) == Decimal("1.6")
    assert Decimal(summary["uncached_token_cost_upper_bound_usd"]) <= runner.CAP


@pytest.mark.parametrize("payload", [
    httpx.ReadTimeout("secret-bearing transport text"),
    response(usage=None), response(service_tier="priority"), response(service_tier=None),
    response(usage={"prompt_tokens": True, "completion_tokens": 20, "total_tokens": 21}),
    response(usage={"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 121}),
    response(usage={"prompt_tokens": 1_047_577, "completion_tokens": 0, "total_tokens": 1_047_577}),
    response(usage={"prompt_tokens": 1, "completion_tokens": 1025, "total_tokens": 1026}),
    response(usage={"prompt_tokens": -1, "completion_tokens": 0, "total_tokens": -1}),
])
def test_uncertain_billing_retains_reservation_and_stops(monkeypatch, tmp_path, prepared, payload):
    summary, events, calls, _, _ = run_mock(monkeypatch, tmp_path, prepared, payload)
    assert len(calls) == 1 and summary["status"] == "stopped"
    assert summary["uncertain_charge_reservation_usd"] == "0.43"
    assert summary["uncached_token_cost_upper_bound_usd"] == "0"
    assert "secret-bearing" not in json.dumps(events)


@pytest.mark.parametrize("change", ["refusal", "truncation", "json", "duplicate", "nonfinite", "shape", "choices", "source"])
def test_invalid_output_is_charged_but_never_retried(monkeypatch, tmp_path, prepared, change):
    payload = response()
    message = payload["choices"][0]["message"]
    if change == "refusal":
        message["refusal"] = "cannot comply"
    elif change == "truncation":
        payload["choices"][0]["finish_reason"] = "length"
    elif change == "json":
        message["content"] = "not JSON"
    elif change == "duplicate":
        message["content"] = '{"findings":[],"findings":[]}'
    elif change == "nonfinite":
        message["content"] = '{"findings":NaN}'
    elif change == "shape":
        message["content"] = '{"findings":[]}'
    elif change == "choices":
        payload["choices"] = []
    else:
        _plan, jobs = subset(prepared)
        shown = jobs[0][1]
        message["content"] = json.dumps({"findings": [{"kind": "assessment",
            "property_key": shown.properties[0].key, "scope_ref": shown.scopes[0].ref,
            "target_ref": None, "stance": "asserted",
            "evidence": {"turn_index": 63, "quote": "forged"},
            "value": {"turn_index": 63, "quote": "forged"}}]})
    summary, events, calls, _, _ = run_mock(monkeypatch, tmp_path, prepared, payload)
    assert len(calls) == 1 and summary["status"] == "stopped"
    assert summary["accepted_offline_proposals"] == 0
    assert summary["uncached_token_cost_upper_bound_usd"] == "0.000072"
    assert summary["uncertain_charge_reservation_usd"] == "0"
    assert events[-1]["provider_response"] == payload


def test_no_call_controls_never_invoke_provider(monkeypatch, tmp_path, prepared):
    plan, jobs = subset(prepared, controls=True)
    monkeypatch.setattr(runner, "fetch", lambda *args: pytest.fail("no-call controls must skip"))
    summary = runner.execute(plan, jobs, approved_digest=plan["execution_sha256"],
                             client=None, api_key="test", output_root=tmp_path)
    assert summary["provider_attempts"] == 0 and summary["no_call_controls_seen"] == 2


def test_disk_failure_before_reservation_prevents_request(monkeypatch, tmp_path, prepared):
    plan, jobs = subset(prepared)
    monkeypatch.setattr(runner.os, "fsync", lambda fd: (_ for _ in ()).throw(OSError("disk failure")))
    monkeypatch.setattr(runner, "fetch", lambda *args: pytest.fail("must persist before request"))
    with pytest.raises(OSError):
        runner.execute(plan, jobs, approved_digest=plan["execution_sha256"],
                       client=None, api_key="test", output_root=tmp_path)
    assert (tmp_path / f'run-{plan["execution_sha256"]}').exists()


def test_reservation_sync_failure_prevents_dispatch(monkeypatch, tmp_path, prepared):
    plan, jobs = subset(prepared)
    original_sync = runner.os.fsync
    syncs = []

    def sync(fd):
        syncs.append(fd)
        if len(syncs) == 2:  # Manifest synced; reservation fails to sync.
            raise OSError("reservation disk failure")
        original_sync(fd)

    monkeypatch.setattr(runner.os, "fsync", sync)
    monkeypatch.setattr(runner, "fetch", lambda *args: pytest.fail("unpersisted reservation must not dispatch"))
    with pytest.raises(OSError):
        runner.execute(plan, jobs, approved_digest=plan["execution_sha256"],
                       client=None, api_key="test", output_root=tmp_path)
    assert len(syncs) == 2


def test_interruption_keeps_one_shot_lock_and_unsettled_reservation(monkeypatch, tmp_path, prepared):
    plan, jobs = subset(prepared)
    monkeypatch.setattr(runner, "fetch", lambda *args: (_ for _ in ()).throw(KeyboardInterrupt()))
    with pytest.raises(KeyboardInterrupt):
        runner.execute(plan, jobs, approved_digest=plan["execution_sha256"],
                       client=None, api_key="test", output_root=tmp_path)
    directory = tmp_path / f'run-{plan["execution_sha256"]}'
    assert "reserved_before_dispatch" in (directory / "journal.jsonl").read_text(encoding="utf-8")
    assert not (directory / "summary.json").exists()
    with pytest.raises(FileExistsError):
        runner.execute(plan, jobs, approved_digest=plan["execution_sha256"],
                       client=None, api_key="test", output_root=tmp_path)


@pytest.mark.parametrize("status", [302, 400, 401, 429, 500])
def test_http_failure_has_one_attempt_and_no_redirect_or_sensitive_logs(status):
    seen = []

    def handle(request):
        seen.append(request)
        assert request.url == "https://api.openai.com/v1/chat/completions"
        return httpx.Response(status, headers={"Location": "https://evil.invalid/"}, text="secret response")

    with (
        httpx.Client(transport=httpx.MockTransport(handle), follow_redirects=False) as client,
        pytest.raises(ValueError, match=f"^http_status_{status}$"),
    ):
        runner.fetch(client, {"model": "test"}, "private-key")
    assert len(seen) == 1


@pytest.mark.parametrize("body", [b"{}", b"[]", b"not JSON", b'{"model":1,"model":2}', b"x" * (runner.MAX_RESPONSE_BYTES + 1)],
                         ids=["empty", "array", "invalid-json", "duplicate-key", "oversized"])
def test_bad_response_envelope_is_rejected(body):
    with (
        httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200, content=body))) as client,
        pytest.raises(ValueError),
    ):
        runner.fetch(client, {}, "private-key")


def test_cli_default_is_offline_and_execute_needs_exact_new_approval(monkeypatch, capsys, prepared):
    monkeypatch.setattr(runner, "prepare_execution", lambda: prepared)
    monkeypatch.setattr(httpx, "Client", lambda *args, **kwargs: pytest.fail("must not create provider client"))
    monkeypatch.setattr("sys.argv", ["run"])
    runner.main()
    assert json.loads(capsys.readouterr().out)["execution_sha256"] == prepared[0]["execution_sha256"]
    monkeypatch.setattr("sys.argv", ["run", "--execute", "--approved-execution", runner.APPROVED_BASE])
    with pytest.raises(SystemExit) as exc:
        runner.main()
    assert exc.value.code == 1
