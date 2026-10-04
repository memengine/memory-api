"""Order/transport safety checks only; mock outputs are not model-quality evidence."""
import json
from copy import deepcopy
from pathlib import Path

import httpx
import pytest

from benchmarks.internal import preference_exploratory_run as runner
from benchmarks.internal import preference_schema_order_diagnostic as probe


@pytest.fixture(scope="module")
def prepared():
    return probe.prepare()


def payload(reply=None):
    return {
        "model": "gpt-4.1-mini-2025-04-14", "service_tier": "default",
        "usage": {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120},
        "choices": [{"finish_reason": "stop", "message": {"role": "assistant",
            "content": json.dumps(reply or {"findings": [{"kind": "abstain", "reason": "not_preference"}]})}}],
    }


def fields(body):
    return runner.parse_json(body.decode("utf-8"))["response_format"]["json_schema"]["schema"]["$defs"]["Assessment"]["properties"]


def test_fixed_preparation_binds_bytes_without_claiming_accuracy(prepared):
    plan, jobs, bodies = prepared
    assert probe.prepare() == prepared
    assert len(jobs) == len(bodies) == len(plan["cases"]) == plan["request_count"] == 6
    assert plan["case_count"] == 3 and plan["cap_usd"] == "0.50"
    assert [row["source_case_id"] for row in plan["cases"]] == ["d0001"] * 2 + ["d0121"] * 2 + ["d0151"] * 2
    assert [row["condition"] for row in plan["cases"]] == list(probe.CONDITIONS) * 3
    assert plan["source_kind"] == "client_assertion"
    assert plan["schema_prompt_text_unchanged"] is True
    for flag in ("semantic_accuracy_measured", "human_release_gate_met", "production_enablement_allowed",
                 "holdouts_loaded", "ai_annotations_loaded"):
        assert plan[flag] is False
    for (key, shown, request), row in zip(jobs, plan["cases"], strict=True):
        assert runner.prep.digest(bodies[key].decode("utf-8")) == row["wire_sha256"]
        assert runner.prep.digest(runner.prep.canonical(request)) == row["request_sha256"]
        assert list(fields(bodies[key])) == row["assessment_property_order"]
        assert all(turn.source_kind == "client_assertion" for turn in shown.turns if turn.role == "user")


def test_only_assessment_property_order_changes_in_actual_wire_bytes(prepared):
    plan, jobs, bodies = prepared
    _, diagnostic_jobs = probe.diagnostic.prepare()
    baseline = {key.removesuffix("_strict_client"): request for key, _, request in diagnostic_jobs if key.endswith("_strict_client")}
    for start in (0, 2, 4):
        key, shown, request = jobs[start]
        other_key, other_shown, other_request = jobs[start + 1]
        control, treatment = bodies[key], bodies[other_key]
        assert shown == other_shown and request == other_request
        assert control == runner.prep.canonical(baseline[plan["cases"][start]["source_case_id"]]).encode("utf-8")
        assert runner.parse_json(control.decode("utf-8")) == runner.parse_json(treatment.decode("utf-8")) == request
        assert list(fields(control)) == ["evidence", "kind", "property_key", "scope_ref", "stance", "target_ref", "value"]
        assert list(fields(treatment)) == ["kind", "evidence", "property_key", "scope_ref", "stance", "target_ref", "value"]
        serialize = lambda value: json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        control_fields, treatment_fields = serialize(fields(control)), serialize(fields(treatment))
        assert treatment.count(treatment_fields) == control.count(control_fields) == 1
        assert treatment.replace(treatment_fields, control_fields, 1) == control
        assert plan["cases"][start]["request_sha256"] == plan["cases"][start + 1]["request_sha256"]
        assert plan["cases"][start]["wire_sha256"] != plan["cases"][start + 1]["wire_sha256"]
        # Includes unchanged required arrays, union branch order, schema-text prompt,
        # target identity, source trust, role, model and sampling/billing settings.


def test_prepare_reads_no_results_annotations_or_holdouts(monkeypatch):
    original = Path.read_text

    def read(path, *args, **kwargs):
        assert "artifacts" not in path.parts and "holdout" not in path.parts
        assert "review-a" not in path.name and "review-b" not in path.name
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", read)
    monkeypatch.setattr(httpx, "Client", lambda *args, **kwargs: pytest.fail("preparation must be offline"))
    probe.prepare()


def test_mock_transport_receives_exact_frozen_bytes_and_consumes_one_authorization(tmp_path, prepared):
    plan, jobs, bodies = prepared
    seen = []

    def handle(request):
        index = len(seen)
        assert request.url == "https://api.openai.com/v1/chat/completions"
        assert request.content == bodies[jobs[index][0]]
        path = tmp_path / f'run-{plan["execution_sha256"]}' / "journal.jsonl"
        assert json.loads(path.read_text(encoding="utf-8").splitlines()[-1])["event"] == "reserved_before_dispatch"
        seen.append(request.content)
        return httpx.Response(200, json=payload())

    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        summary = runner.execute(plan, jobs, approved_digest=plan["execution_sha256"], client=client,
            api_key="private-test", cap=probe.diagnostic.CAP, output_root=tmp_path, wire_bodies=bodies)
        assert summary["status"] == "complete" and len(seen) == 6
        assert summary["validator_passed_responses"] == summary["abstention_findings"] == 6
        assert summary["assessment_findings"] == 0
        with pytest.raises(FileExistsError):
            runner.execute(plan, jobs, approved_digest=plan["execution_sha256"], client=client,
                api_key="private-test", cap=probe.diagnostic.CAP, output_root=tmp_path, wire_bodies=bodies)
    assert len(seen) == 6
    directory = tmp_path / f'run-{plan["execution_sha256"]}'
    assert all("private-test" not in path.read_text(encoding="utf-8") for path in directory.iterdir())


@pytest.mark.parametrize("change", ["missing", "extra", "order", "content", "mutable", "absent", "hash"])
def test_bad_wire_is_rejected_before_lock_or_dispatch(monkeypatch, tmp_path, prepared, change):
    plan, jobs, bodies = deepcopy(prepared)
    key = jobs[0][0]
    if change == "missing":
        bodies.pop(key)
    elif change == "extra":
        bodies["extra"] = b"{}"
    elif change == "order":
        bodies[key] = bodies[jobs[1][0]]  # Same canonical request, wrong exact bytes.
    elif change == "content":
        request = runner.parse_json(bodies[key].decode("utf-8"))
        request["temperature"] = 1
        bodies[key] = runner.prep.canonical(request).encode("utf-8")
        # Even a resealed byte hash cannot conceal a request-meaning mismatch.
        plan["cases"][0]["wire_sha256"] = runner.prep.digest(bodies[key].decode("utf-8"))
        plan.pop("execution_sha256")
        plan["execution_sha256"] = runner.prep.digest(runner.prep.canonical(plan))
    elif change == "mutable":
        bodies[key] = bytearray(bodies[key])
    elif change == "absent":
        bodies = None
    else:
        plan["cases"][0].pop("wire_sha256")
        plan.pop("execution_sha256")
        plan["execution_sha256"] = runner.prep.digest(runner.prep.canonical(plan))
    monkeypatch.setattr(runner, "fetch", lambda *args, **kwargs: pytest.fail("bad wire must not dispatch"))
    with pytest.raises(ValueError):
        runner.execute(plan, jobs, approved_digest=plan["execution_sha256"], client=None, api_key="test",
            cap=probe.diagnostic.CAP, output_root=tmp_path, wire_bodies=bodies)
    assert not list(tmp_path.iterdir())


def test_wire_mapping_is_snapshotted_before_dispatch(monkeypatch, tmp_path, prepared):
    plan, jobs, bodies = deepcopy(prepared)
    expected = dict(bodies)
    seen = []

    def fetch(client, request, key, *, wire_body):
        assert wire_body == expected[jobs[len(seen)][0]]
        bodies.clear()
        seen.append(wire_body)
        return payload()

    monkeypatch.setattr(runner, "fetch", fetch)
    summary = runner.execute(plan, jobs, approved_digest=plan["execution_sha256"], client=None, api_key="test",
        cap=probe.diagnostic.CAP, output_root=tmp_path, wire_bodies=bodies)
    assert summary["status"] == "complete" and len(seen) == 6


def test_wrong_target_is_still_rejected_and_charged_once(monkeypatch, tmp_path, prepared):
    plan, jobs, bodies = prepared
    calls = []
    reply = {"findings": [{"kind": "assessment", "property_key": "examples.language", "scope_ref": "global",
        "stance": "replacement", "target_ref": "t_other", "evidence": {"turn_index": 1, "quote": jobs[0][1].turns[1].content},
        "value": {"turn_index": 1, "quote": "Python"}}]}
    monkeypatch.setattr(runner, "fetch", lambda *args, **kwargs: (calls.append(1), payload(reply))[1])
    summary = runner.execute(plan, jobs, approved_digest=plan["execution_sha256"], client=None, api_key="test",
        cap=probe.diagnostic.CAP, output_root=tmp_path, wire_bodies=bodies)
    assert len(calls) == 1 and summary["stop_reason"] == "frozen_validator_rejected"
    assert summary["assessment_findings"] == 0 and summary["uncached_token_cost_upper_bound_usd"] == "0.000072"
    path = tmp_path / f'run-{plan["execution_sha256"]}' / "journal.jsonl"
    assert json.loads(path.read_text(encoding="utf-8").splitlines()[-1])["decision"]["reasons"] == ["target_identity_mismatch"]


def test_cli_is_offline_and_rejects_consumed_approvals(monkeypatch, capsys, prepared):
    monkeypatch.setattr(probe, "prepare", lambda: prepared)
    monkeypatch.setattr(httpx, "Client", lambda *args, **kwargs: pytest.fail("unapproved CLI must not construct provider client"))
    monkeypatch.setattr("sys.argv", ["order-probe"])
    probe.main()
    assert json.loads(capsys.readouterr().out)["request_count"] == 6
    for approval in (probe.diagnostic.ORIGINAL_EXECUTION, probe.CONSUMED_DIAGNOSTIC):
        monkeypatch.setattr("sys.argv", ["order-probe", "--execute", "--approved-execution", approval])
        with pytest.raises(SystemExit) as exc:
            probe.main()
        assert exc.value.code == 1
