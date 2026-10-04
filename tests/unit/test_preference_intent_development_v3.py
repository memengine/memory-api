"""Offline preparation and mocked execution; no paid calls or semantic labels."""
import json
from copy import deepcopy
from pathlib import Path

import httpx
import pytest

from benchmarks.internal import preference_exploratory_run as runner
from benchmarks.internal import preference_intent_development_v3 as probe
from benchmarks.internal import preference_semantic_intent_v3 as intent
from tests.unit.test_preference_schema_order_diagnostic import payload


@pytest.fixture(scope="module")
def prepared():
    return probe.prepare()


def reseal(plan):
    plan.pop("execution_sha256", None)
    plan["execution_sha256"] = runner.prep.digest(runner.prep.canonical(plan))


def finding(shown, *, semantic_intent="adopts_value", value="Python", scope="global"):
    index = next(i for i, turn in enumerate(shown.context.turns) if turn.role == "user")
    return {"findings": [{"kind": "assessment", "property_key": "examples.language", "scope_ref": scope,
        "intent": semantic_intent, "evidence": {"turn_index": index, "quote": shown.context.turns[index].content},
        "value": {"turn_index": index, "quote": value} if value else None}]}


def execute(prepared, tmp_path, client=None):
    plan, jobs, bodies = prepared
    return runner.execute(plan, jobs, approved_digest=plan["execution_sha256"], client=client,
        api_key="private-test", output_root=tmp_path, cap=probe.previous.CAP, wire_bodies=bodies)


def test_fixed_public_coverage_and_new_seals_have_no_execution_permission(prepared):
    plan, jobs, bodies = prepared
    assert len(jobs) == len(bodies) == plan["case_count"] == plan["request_count"] == 24
    assert [job[0] for job in jobs[:3]] == ["d0001", "d0121", "d0151"]
    assert plan["language_counts"] == {"en": 8, "hi": 8, "hinglish": 8}
    assert plan["related_scenario_count"] == 8
    assert plan["mode"] == "preference_intent_development_v3"
    assert plan["validator"] == runner.INTENT_VALIDATOR
    assert plan["early_stop"] == runner.INTENT_EARLY_STOP
    assert plan["cap_usd"] == "0.50" and plan["per_attempt_reservation_usd"] == "0.43"
    assert plan["model_calls"] == plan["actual_provider_cost_usd"] == 0
    assert plan["preparation_is_not_provider_acceptance"] is True
    for flag in ("provider_execution_approved", "request_schema_provider_verified", "semantic_accuracy_measured",
        "human_release_gate_met", "production_enablement_allowed", "holdouts_loaded", "ai_annotations_loaded",
        "sql_completeness_verified", "sql_concurrency_verified", "candidate_routes_are_write_permission"):
        assert plan[flag] is False
    assert set(plan["consumed_authorizations_not_reused"]) == set(probe.CONSUMED)
    assert plan["execution_sha256"] not in probe.CONSUMED
    assert plan["execution_sha256"] == runner.prep.digest(runner.prep.canonical({
        key: value for key, value in plan.items() if key != "execution_sha256"}))
    for path in runner.INTENT_VALIDATION_SOURCES:
        assert plan["source_sha256_utf8_lf"][path] == runner.prep.digest((runner.prep.ROOT / path).read_text(encoding="utf-8"))
    for (case_id, shown, request), row in zip(jobs, plan["cases"], strict=True):
        assert row["case_id"] == case_id
        assert row["wire_sha256"] == runner.prep.digest(bodies[case_id].decode("utf-8"))
        assert row["request_sha256"] == runner.prep.digest(runner.prep.canonical(request))
        assert row["context_sha256"] == runner.prep.digest(runner.prep.canonical(shown.model_dump(mode="json")))
        assert runner.parse_json(bodies[case_id].decode("utf-8")) == request
        assert request["model"] == "gpt-4.1-mini-2025-04-14" and request["service_tier"] == "default"


def test_schema_adapter_changes_only_the_union_and_preserves_exact_key_order(prepared):
    _, jobs, _ = prepared
    for _, shown, request in jobs:
        original = intent.response_schema(shown)
        schema = deepcopy(request["response_format"]["json_schema"]["schema"])
        items = schema["properties"]["findings"]["items"]
        assert "oneOf" not in items and "discriminator" not in items
        items["oneOf"] = items.pop("anyOf")
        items["discriminator"] = original["properties"]["findings"]["items"]["discriminator"]
        for name in ("IntentFinding", "Abstention"):
            tag = schema["$defs"][name]["properties"]["kind"]
            tag["const"] = tag.pop("enum")[0]
        assert schema == original
        definition = schema["$defs"]["IntentFinding"]
        assert list(definition["properties"]) == definition["required"] == [
            "kind", "property_key", "scope_ref", "intent", "evidence", "value"]


def test_projection_has_full_source_but_no_backend_fields_or_answer_hints(prepared):
    _, jobs, _ = prepared
    for case_id, shown, request in jobs:
        projection = runner.parse_json(request["messages"][1]["content"])
        assert projection == intent.model_input(shown)
        text = runner.prep.canonical(projection)
        for field in ("tenant_id", "profile_id", "memory_id", "authority", "version", "covered_identities",
            "case_id", "family", "scenario_group", "candidate_route", "target_ref"):
            assert field not in text
        assert case_id not in text
        assert request["messages"][0]["content"] == probe.INSTRUCTIONS.read_text(encoding="utf-8")
    assert any("मैंने" in request["messages"][1]["content"] for _, _, request in jobs)


def test_preparation_only_reads_fixed_public_assets_and_never_creates_client(monkeypatch):
    allowed = {probe.INSTRUCTIONS, probe.previous.FREEZE, probe.previous.INSTRUCTIONS, runner.prep.PROTOCOL,
        *(runner.prep.ROOT / path for path in probe.previous.FROZEN_PATHS),
        *(runner.prep.ROOT / path for path in probe.previous.SOURCE_PATHS),
        *(runner.prep.ROOT / path for path in runner.INTENT_VALIDATION_SOURCES)}
    original = Path.read_text

    def read(path, *args, **kwargs):
        assert path in allowed, f"unexpected read: {path}"
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", read)
    monkeypatch.setattr(httpx, "Client", lambda *a, **kw: pytest.fail("preparation created a client"))
    probe.prepare()


def test_prepared_stop_policy_does_not_alias_the_fixed_runner_policy():
    plan, _, _ = probe.prepare()
    plan["early_stop"]["first_case_ids"].clear()
    assert runner.INTENT_EARLY_STOP["first_case_ids"] == ["d0001", "d0121", "d0151"]


@pytest.mark.parametrize("field,value", [("model", "another-model"), ("max_completion_tokens", 2048), ("stream", True)])
def test_even_resealed_request_cannot_change_fixed_candidate_bounds(monkeypatch, tmp_path, prepared, field, value):
    plan, jobs, bodies = deepcopy(prepared)
    case_id = jobs[0][0]
    jobs[0][2][field] = value
    bodies[case_id] = json.dumps(jobs[0][2], ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    plan["cases"][0]["request_sha256"] = runner.prep.digest(runner.prep.canonical(jobs[0][2]))
    plan["cases"][0]["wire_sha256"] = runner.prep.digest(bodies[case_id].decode("utf-8"))
    reseal(plan)
    monkeypatch.setattr(runner, "fetch", lambda *a, **kw: pytest.fail("changed candidate dispatched"))
    with pytest.raises(ValueError):
        execute((plan, jobs, bodies), tmp_path)
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("change", ["source", "mode", "validator", "early_stop", "case_count", "cap", "permission", "context", "wire", "body_order", "request"])
def test_changed_plan_or_wire_blocks_before_lock_or_dispatch(monkeypatch, tmp_path, prepared, change):
    plan, jobs, bodies = deepcopy(prepared)
    if change == "source":
        plan["source_sha256_utf8_lf"]["api/services/evidence_policy.py"] = "f" * 64
    elif change == "mode":
        plan["mode"] = "preference_binding_development_v2"
    elif change == "validator":
        plan["validator"] = runner.BINDING_VALIDATOR
    elif change == "early_stop":
        plan["early_stop"] = {"first_case_ids": [], "stop_if_all_have_zero_usable_candidates": False}
    elif change == "case_count":
        plan["case_count"] = 25
    elif change == "cap":
        plan["cap_usd"] = "2.00"
    elif change == "permission":
        plan["candidate_routes_are_write_permission"] = True
    elif change == "context":
        plan["cases"][0]["context_sha256"] = "f" * 64
    elif change == "wire":
        plan["cases"][0]["wire_sha256"] = "f" * 64
    elif change == "body_order":
        bodies[jobs[0][0]] = runner.prep.canonical(jobs[0][2]).encode("utf-8")
    else:
        jobs[0][2]["messages"][0]["content"] += " changed"
    reseal(plan)
    monkeypatch.setattr(runner, "fetch", lambda *a, **kw: pytest.fail("invalid plan dispatched"))
    with pytest.raises(ValueError):
        execute((plan, jobs, bodies), tmp_path)
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("schema_change", ["kind", "union"])
def test_changed_schema_is_not_silently_adapted(monkeypatch, prepared, schema_change):
    original = intent.response_schema

    def changed(shown):
        schema = original(shown)
        if schema_change == "kind":
            schema["$defs"]["IntentFinding"]["properties"]["kind"]["const"] = "abstain"
        else:
            schema["properties"]["findings"]["items"]["oneOf"].reverse()
        return schema

    monkeypatch.setattr(intent, "response_schema", changed)
    with pytest.raises(ValueError):
        probe.prepare()


def test_all_review_proposals_stop_after_three_without_becoming_a_success_metric(monkeypatch, tmp_path, prepared):
    _, jobs, bodies = prepared
    calls = []

    def fetch(client, request, key, *, wire_body):
        i = len(calls)
        assert wire_body == bodies[jobs[i][0]]
        calls.append(wire_body)
        return payload(finding(jobs[i][1], semantic_intent="unclear", value=None))

    monkeypatch.setattr(runner, "fetch", fetch)
    summary = execute(prepared, tmp_path)
    assert summary["stop_reason"] == "intent_smoke_no_usable_candidates"
    assert len(calls) == summary["intent_proposal_findings"] == summary["validator_passed_responses"] == 3
    assert summary["candidate_route_counts"] == {"review_required": 3}
    assert summary["usable_candidate_findings"] == 0
    assert summary["semantic_accuracy_measured"] is summary["candidate_routes_are_write_permission"] is False
    with pytest.raises(FileExistsError):
        execute(prepared, tmp_path)
    assert len(calls) == 3


def test_mock_new_replacement_and_scoped_review_keep_intent_and_route_separate(tmp_path, prepared):
    plan, jobs, bodies = prepared
    calls = []

    def handle(request):
        i = len(calls)
        assert request.content == bodies[jobs[i][0]]
        journal = tmp_path / f'run-{plan["execution_sha256"]}' / "journal.jsonl"
        assert json.loads(journal.read_text(encoding="utf-8").splitlines()[-1])["event"] == "reserved_before_dispatch"
        calls.append(request.content)
        reply = None if i >= 3 else finding(jobs[i][1], semantic_intent="explicit_change" if i == 1 else "adopts_value",
            scope="atlas" if i == 2 else "global")
        return httpx.Response(200, json=payload(reply))

    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        summary = execute(prepared, tmp_path, client)
    assert summary["status"] == "complete" and len(calls) == 24
    assert summary["candidate_route_counts"] == {"new_candidate": 1, "replacement_candidate": 1, "review_required": 1}
    assert summary["usable_candidate_findings"] == 2 and summary["intent_proposal_findings"] == 3
    directory = tmp_path / f'run-{plan["execution_sha256"]}'
    events = [json.loads(line) for line in (directory / "journal.jsonl").read_text(encoding="utf-8").splitlines()]
    response = next(event for event in events if event["event"] == "response")
    assert response["reply"]["findings"][0]["intent"] == "adopts_value"
    assert response["decision"]["proposals"][0]["target_ref"] is None
    assert "assessments" not in response["decision"]
    scoped = next(event for event in events if event.get("case_id") == "d0151" and event["event"] == "response")
    assert scoped["decision"]["proposals"][0]["target_ref"] == "t_project"
    assert scoped["decision"]["proposals"][0]["intent"] == "adopts_value"
    assert scoped["decision"]["proposals"][0]["candidate_route"] == "review_required"
    assert summary["journal_sha256"] == runner.prep.digest((directory / "journal.jsonl").read_text(encoding="utf-8"))
    assert all("private-test" not in path.read_text(encoding="utf-8") for path in directory.iterdir())


@pytest.mark.parametrize("change", ["old_stance", "backend_route", "invalid_evidence"])
def test_invalid_reply_is_recorded_charged_once_and_never_repaired(monkeypatch, tmp_path, prepared, change):
    plan, jobs, _ = prepared
    reply = finding(jobs[0][1])
    raw = reply["findings"][0]
    if change == "old_stance":
        raw["stance"] = "replacement"
        del raw["intent"]
    elif change == "backend_route":
        raw["candidate_route"] = "replacement_candidate"
    else:
        raw["evidence"]["quote"] = "fabricated"
    calls = []
    monkeypatch.setattr(runner, "fetch", lambda *a, **kw: (calls.append(1), payload(reply))[1])
    summary = execute(prepared, tmp_path)
    assert len(calls) == 1 and summary["stop_reason"] == "frozen_validator_rejected"
    assert summary["intent_proposal_findings"] == summary["usable_candidate_findings"] == 0
    assert summary["uncached_token_cost_upper_bound_usd"] == "0.000072"
    event = json.loads((tmp_path / f'run-{plan["execution_sha256"]}' / "journal.jsonl").read_text(encoding="utf-8").splitlines()[-1])
    assert event["reply"] == reply and event["decision"]["status"] == "rejected"


def test_timeout_keeps_reservation_and_consumes_authorization(monkeypatch, tmp_path, prepared):
    calls = []

    def fetch(*a, **kw):
        calls.append(1)
        raise httpx.ReadTimeout("secret-bearing details")

    monkeypatch.setattr(runner, "fetch", fetch)
    summary = execute(prepared, tmp_path)
    assert len(calls) == 1 and summary["stop_reason"] == "ReadTimeout"
    assert summary["uncertain_charge_reservation_usd"] == "0.43"
    with pytest.raises(FileExistsError):
        execute(prepared, tmp_path)


def test_plan_jobs_and_wire_are_snapshotted_before_mock_dispatch(monkeypatch, tmp_path, prepared):
    plan, jobs, bodies = deepcopy(prepared)
    calls = []

    def fetch(client, request, key, *, wire_body):
        jobs.clear()
        plan["cases"].clear()
        bodies.clear()
        calls.append(wire_body)
        return payload()

    monkeypatch.setattr(runner, "fetch", fetch)
    summary = execute((plan, jobs, bodies), tmp_path)
    assert len(calls) == 3 and summary["stop_reason"] == "intent_smoke_no_usable_candidates"


def test_preparation_cli_has_no_execution_option(monkeypatch):
    import sys

    monkeypatch.setattr(sys, "argv", ["prepare", "--execute"])
    with pytest.raises(SystemExit) as exc:
        probe.main()
    assert exc.value.code == 2
