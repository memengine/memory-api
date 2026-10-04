"""Mocked diagnostic control-flow tests, never semantic accuracy evidence."""
import json
from copy import deepcopy
from decimal import Decimal
from pathlib import Path

import httpx
import pytest

from benchmarks.internal import preference_abstention_diagnostic as diagnostic
from benchmarks.internal import preference_exploratory_run as runner


@pytest.fixture(scope="module")
def prepared():
    return diagnostic.prepare()


def payload(reply=None, *, prompt=100):
    return {
        "model": "gpt-4.1-mini-2025-04-14", "service_tier": "default",
        "usage": {"prompt_tokens": prompt, "completion_tokens": 20, "total_tokens": prompt + 20},
        "choices": [{"finish_reason": "stop", "message": {"role": "assistant", "refusal": None,
                    "content": json.dumps(reply or {"findings": [{"kind": "abstain", "reason": "not_preference"}]})}}],
    }


def test_fixed_plan_is_deterministic_and_has_no_release_authority(prepared):
    plan, jobs = prepared
    assert plan == diagnostic.prepare()[0]
    assert plan["cap_usd"] == "0.50"
    assert plan["request_count"] == len(jobs) == len(plan["cases"]) == 9
    assert [row["source_case_id"] for row in plan["cases"]] == ["d0001"] * 3 + ["d0121"] * 3 + ["d0151"] * 3
    assert [row["condition"] for row in plan["cases"]] == list(diagnostic.CONDITIONS) * 3
    for flag in ("semantic_accuracy_measured", "human_release_gate_met", "production_enablement_allowed",
                 "holdouts_loaded", "ai_annotations_loaded"):
        assert plan[flag] is False


def test_each_comparison_changes_only_its_declared_factor(prepared):
    _, jobs = prepared
    for start in (0, 3, 6):
        _, control_context, control = jobs[start]
        _, json_context, json_request = jobs[start + 1]
        _, direct_context, direct = jobs[start + 2]
        assert control_context == json_context
        expected_json = deepcopy(control)
        expected_json["response_format"] = {"type": "json_object"}
        assert json_request == expected_json
        assert direct["response_format"] == control["response_format"]
        assert direct["messages"][0] == control["messages"][0]
        schema = runner.prep.canonical(control["response_format"]["json_schema"]["schema"])
        assert schema in control["messages"][0]["content"]
        assert diagnostic.SCHEMA_PREFIX in control["messages"][0]["content"]
        expected_source = json.loads(control["messages"][1]["content"])
        for turn in expected_source["turns"]:
            if turn["role"] == "user":
                turn["source_kind"] = "direct_user_input"
        assert direct["messages"][1]["content"] == runner.prep.canonical(expected_source)
        assert {k: v for k, v in direct.items() if k != "messages"} == {k: v for k, v in control.items() if k != "messages"}
        assert direct_context.principal == control_context.principal
        assert direct_context.targets == control_context.targets
        assert direct_context.properties == control_context.properties
        assert direct_context.scopes == control_context.scopes
        for before, after in zip(control_context.turns, direct_context.turns, strict=True):
            assert before.content == after.content and before.role == after.role
            assert before.turn_id == after.turn_id


def test_preparation_does_not_read_annotations_predictions_or_holdouts(monkeypatch):
    read = Path.read_text

    def guarded(path, *args, **kwargs):
        assert "artifacts" not in path.parts
        assert "holdout" not in path.parts
        assert "review-a" not in path.name and "review-b" not in path.name
        return read(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", guarded)
    diagnostic.prepare()


def test_mixed_findings_are_not_confused_with_validator_passes(monkeypatch, tmp_path, prepared):
    plan, jobs = prepared
    calls = []
    user = jobs[0][1].turns[1].content
    preference = {"findings": [{"kind": "assessment", "property_key": "examples.language",
        "scope_ref": "global", "target_ref": None, "stance": "asserted",
        "evidence": {"turn_index": 1, "quote": user}, "value": {"turn_index": 1, "quote": "Python"}}]}

    def fetch(client, request, key):
        calls.append(request)
        return payload(preference if len(calls) == 1 else None)

    monkeypatch.setattr(runner, "fetch", fetch)
    summary = runner.execute(plan, jobs, approved_digest=plan["execution_sha256"],
                             client=None, api_key="test", cap=diagnostic.CAP, output_root=tmp_path)
    assert summary["status"] == "complete" and len(calls) == 9
    assert summary["validator_passed_responses"] == 9
    assert summary["responses_with_assessments"] == summary["assessment_findings"] == 1
    assert summary["abstention_findings"] == 8
    assert "accepted_offline_proposals" not in summary
    with pytest.raises(FileExistsError):
        runner.execute(plan, jobs, approved_digest=plan["execution_sha256"],
                       client=None, api_key="test", cap=diagnostic.CAP, output_root=tmp_path)
    assert len(calls) == 9


@pytest.mark.parametrize("failure", ["timeout", "usage", "shape"])
def test_failure_stops_diagnostic_without_retry(monkeypatch, tmp_path, prepared, failure):
    plan, jobs = prepared
    calls = []

    def fetch(*args):
        calls.append(1)
        if failure == "timeout":
            raise httpx.ReadTimeout("private transport detail")
        result = payload()
        if failure == "usage":
            result["usage"] = None
        else:
            result["choices"][0]["message"]["content"] = '{}'
        return result

    monkeypatch.setattr(runner, "fetch", fetch)
    summary = runner.execute(plan, jobs, approved_digest=plan["execution_sha256"],
                             client=None, api_key="test", cap=diagnostic.CAP, output_root=tmp_path)
    assert len(calls) == 1 and summary["status"] == "stopped"
    assert summary["uncertain_charge_reservation_usd"] == ("0" if failure == "shape" else "0.43")


def test_half_dollar_cap_uses_existing_worst_case_reservation(monkeypatch, tmp_path, prepared):
    plan, jobs = prepared
    calls = []
    monkeypatch.setattr(runner, "fetch", lambda *args: (calls.append(1), payload(prompt=1_047_576))[1])
    summary = runner.execute(plan, jobs, approved_digest=plan["execution_sha256"],
                             client=None, api_key="test", cap=diagnostic.CAP, output_root=tmp_path)
    assert len(calls) == 1
    assert summary["stop_reason"] == "budget_reservation_unavailable"
    assert Decimal(summary["uncached_token_cost_upper_bound_usd"]) <= Decimal("0.50")


@pytest.mark.parametrize("change", ["request", "context", "approval", "cap"])
def test_changed_frozen_diagnostic_cannot_dispatch(monkeypatch, tmp_path, prepared, change):
    plan, jobs = deepcopy(prepared)
    approved = plan["execution_sha256"]
    cap = diagnostic.CAP
    if change == "request":
        jobs[0][2]["temperature"] = 1
    elif change == "context":
        jobs[0] = (jobs[0][0], jobs[0][1].model_copy(update={"targets": ()}), jobs[0][2])
    elif change == "approval":
        approved = diagnostic.ORIGINAL_EXECUTION
    else:
        cap = Decimal("2.00")
    monkeypatch.setattr(runner, "fetch", lambda *args: pytest.fail("changed run must not dispatch"))
    with pytest.raises(ValueError):
        runner.execute(plan, jobs, approved_digest=approved, client=None, api_key="test", cap=cap, output_root=tmp_path)
    assert not list(tmp_path.iterdir())


def test_interrupted_derived_report_keeps_abstentions_and_pending_billing_separate(monkeypatch, tmp_path):
    root = tmp_path / f"run-{diagnostic.ORIGINAL_EXECUTION}"
    root.mkdir()
    path = root / "journal.jsonl"
    events = [
        {"case_id": "d0001", "event": "reserved_before_dispatch"},
        {"case_id": "d0001", "event": "response", "uncached_token_cost_upper_bound_usd": "0.01",
         "decision": {"status": "attributed_proposals_only", "assessments": [], "abstentions": [{"kind": "abstain"}]}},
        {"case_id": "d0002", "event": "reserved_before_dispatch"},
    ]
    # Test-fixture generation only, not an edit to an existing experiment artifact.
    path.write_text("\n".join(json.dumps(e) for e in events) + "\n", encoding="utf-8")
    text = path.read_text(encoding="utf-8")
    monkeypatch.setattr(runner, "OUTPUT_ROOT", tmp_path)
    monkeypatch.setattr(diagnostic, "ORIGINAL_JOURNAL", runner.prep.digest(text))
    report = diagnostic.interrupted_report()
    assert report["validator_passed_responses"] == 1
    assert report["assessment_findings"] == report["responses_with_assessments"] == 0
    assert report["abstention_findings"] == 1
    assert report["requests_without_saved_response"] == ["d0002"]
    assert report["unsettled_reservation_upper_bound_usd"] == "0.43"
    assert path.read_text(encoding="utf-8") == text
    assert not (root / "summary.json").exists()
    monkeypatch.setattr(diagnostic, "ORIGINAL_JOURNAL", "changed")
    with pytest.raises(ValueError, match="interrupted journal changed"):
        diagnostic.interrupted_report()


def test_cli_is_offline_by_default_and_does_not_accept_original_run_approval(monkeypatch, capsys, prepared):
    monkeypatch.setattr(diagnostic, "prepare", lambda: prepared)
    monkeypatch.setattr(diagnostic, "interrupted_report", lambda: {"saved_responses": 319})
    monkeypatch.setattr(httpx, "Client", lambda *args, **kwargs: pytest.fail("offline preparation must not construct a client"))
    monkeypatch.setattr("sys.argv", ["diagnostic"])
    diagnostic.main()
    assert json.loads(capsys.readouterr().out)["plan"]["request_count"] == 9
    monkeypatch.setattr("sys.argv", ["diagnostic", "--execute", "--approved-execution", diagnostic.ORIGINAL_EXECUTION])
    with pytest.raises(SystemExit) as exc:
        diagnostic.main()
    assert exc.value.code == 1
