"""Preparation/transport tests with synthetic mocks, never model-quality evidence."""
import hashlib
import json
from copy import deepcopy
from pathlib import Path

import httpx
import pytest

from benchmarks.internal import preference_binding_development_v2 as probe
from benchmarks.internal import preference_exploratory_run as runner
from benchmarks.internal import preference_semantic_binding_v2 as binding
from tests.unit.test_preference_schema_order_diagnostic import payload


@pytest.fixture(scope="module")
def prepared():
    return probe.prepare()


def reseal(plan):
    plan.pop("execution_sha256", None)
    plan["execution_sha256"] = runner.prep.digest(runner.prep.canonical(plan))


def assessment(shown, *, stance="asserted", scope="global", value="Python"):
    context = shown.context
    index = next(i for i, turn in enumerate(context.turns) if turn.role == "user")
    return {"findings": [{"kind": "assessment", "property_key": "examples.language", "scope_ref": scope,
        "stance": stance, "evidence": {"turn_index": index, "quote": context.turns[index].content},
        "value": {"turn_index": index, "quote": value} if value else None}]}


def execute(prepared, tmp_path, client):
    plan, jobs, bodies = prepared
    return runner.execute(plan, jobs, approved_digest=plan["execution_sha256"], client=client,
                          api_key="private-test", cap=probe.CAP, output_root=tmp_path, wire_bodies=bodies)


def test_preparation_is_deterministic_bounded_and_proposal_only(prepared):
    plan, jobs, bodies = prepared
    assert probe.prepare() == prepared
    assert tuple(job[0] for job in jobs) == probe.CASE_IDS
    assert tuple(job[0] for job in jobs[:3]) == runner.BINDING_EARLY_CASE_IDS
    assert len(jobs) == len(bodies) == len(plan["cases"]) == plan["request_count"] == plan["case_count"] == 24
    assert plan["related_scenario_count"] == 8
    assert plan["language_counts"] == {"en": 8, "hi": 8, "hinglish": 8}
    assert {row["language"]: sum(item["language"] == row["language"] for item in plan["cases"]) for row in plan["cases"]} == plan["language_counts"]
    assert len({row["scenario_group"] for row in plan["cases"]}) == 8
    assert {row["family"] for row in plan["cases"]} == {"adopted_default", "committed_replacement", "scoped_exception",
        "undecided_without_alternative", "tentative_interest", "rejected_alternative", "unrelated_fact_or_question", "ambiguous_or_late_reference"}
    assert plan["validator"] == runner.BINDING_VALIDATOR and plan["cap_usd"] == "0.50"
    assert plan["model_calls"] == plan["actual_provider_cost_usd"] == 0
    for flag in ("provider_execution_approved", "request_schema_provider_verified", "semantic_accuracy_measured",
                 "human_release_gate_met", "production_enablement_allowed", "holdouts_loaded", "ai_annotations_loaded",
                 "sql_completeness_verified", "sql_concurrency_verified", "planning_cost_is_hard_spending_cap"):
        assert plan[flag] is False
    assert set(plan["consumed_authorizations_not_reused"]) == set(probe.CONSUMED)
    for (case_id, shown, request), row in zip(jobs, plan["cases"], strict=True):
        assert binding.complete(shown)
        assert len(shown.covered_identities) == len(shown.context.properties) * len(shown.context.scopes)
        assert all(turn.source_kind == "client_assertion" for turn in shown.context.turns if turn.role == "user")
        assert row["request_sha256"] == runner.prep.digest(runner.prep.canonical(request))
        assert row["context_sha256"] == runner.prep.digest(runner.prep.canonical(shown.model_dump(mode="json")))
        assert row["wire_sha256"] == hashlib.sha256(bodies[case_id]).hexdigest()
        assert runner.parse_json(bodies[case_id].decode("utf-8")) == request
        assert request["model"] == "gpt-4.1-mini-2025-04-14" and request["service_tier"] == "default"
        assert request["temperature"] == 0 and request["max_completion_tokens"] == 1024


def test_provider_schema_changes_only_the_tagged_union(prepared):
    _, jobs, bodies = prepared
    for key, shown, request in jobs:
        original = binding.response_schema(shown)
        schema = deepcopy(request["response_format"]["json_schema"]["schema"])
        items = schema["properties"]["findings"]["items"]
        assert "discriminator" not in items and "oneOf" not in items
        items["oneOf"] = items.pop("anyOf")
        items["discriminator"] = original["properties"]["findings"]["items"]["discriminator"]
        for name in ("Interpretation", "Abstention"):
            tag = schema["$defs"][name]["properties"]["kind"]
            tag["const"] = tag.pop("enum")[0]
        assert schema == original
        wire = runner.parse_json(bodies[key].decode("utf-8"))
        fields = wire["response_format"]["json_schema"]["schema"]["$defs"]["Interpretation"]["properties"]
        assert list(fields) == ["kind", "property_key", "scope_ref", "stance", "evidence", "value"]
        assert "target_ref" not in bodies[key].decode("utf-8")


def test_source_projection_excludes_backend_fields_and_author_tags(prepared):
    _, jobs, _ = prepared
    for key, shown, request in jobs:
        source = runner.parse_json(request["messages"][1]["content"])
        assert source == binding.model_input(shown)
        text = runner.prep.canonical(source)
        for forbidden in ("tenant_id", "profile_id", "memory_id", "version", "authority", "turn_id", "covered_identities",
                          "case_id", "family", "scenario_group", "target_ref", "existing_targets"):
            assert forbidden not in text
        assert key not in text
        assert all("ref" not in target for target in source["existing_preferences"])
        assert request["messages"][0]["content"] == probe.INSTRUCTIONS.read_text(encoding="utf-8")
    assert "मैंने" in jobs[probe.CASE_IDS.index("d0122")][2]["messages"][1]["content"]


def test_preparation_reads_only_fixed_public_assets_and_creates_no_client(monkeypatch):
    allowed = {probe.FREEZE, probe.INSTRUCTIONS, runner.prep.PROTOCOL,
        runner.prep.ROOT / "benchmarks/internal/datasets/preference_semantic/development/inputs-v1.json",
        runner.prep.ROOT / "benchmarks/internal/datasets/preference_semantic/development/manifest-v1.json",
        *(runner.prep.ROOT / path for path in probe.FROZEN_PATHS),
        *(runner.prep.ROOT / path for path in probe.SOURCE_PATHS)}
    original = Path.read_text

    def read(path, *args, **kwargs):
        assert path in allowed, f"unexpected read: {path}"
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", read)
    monkeypatch.setattr(httpx, "Client", lambda *args, **kwargs: pytest.fail("offline preparation must not create client"))
    probe.prepare()


@pytest.mark.parametrize("change", ["asset", "freeze-path", "model", "budget-bounds", "kind", "union"])
def test_changed_frozen_assets_or_candidate_or_union_block_preparation(monkeypatch, change):
    original = Path.read_text

    def read(path, *args, **kwargs):
        text = original(path, *args, **kwargs)
        if change == "asset" and path == runner.prep.ROOT / "benchmarks/internal/preference_semantic_binding_v2.py":
            return text + "\n# changed\n"
        if change == "freeze-path" and path == probe.FREEZE:
            raw = json.loads(text)
            raw["sha256"]["other"] = "f" * 64
            return json.dumps(raw)
        if change in {"model", "budget-bounds"} and path == runner.prep.PROTOCOL:
            raw = json.loads(text)
            raw["candidate"]["model" if change == "model" else "max_completion_tokens"] = "another" if change == "model" else 1025
            return json.dumps(raw)
        return text

    monkeypatch.setattr(Path, "read_text", read)
    if change in {"kind", "union"}:
        original_schema = binding.response_schema

        def schema(shown):
            raw = original_schema(shown)
            if change == "kind":
                raw["$defs"]["Interpretation"]["properties"]["kind"]["const"] = "abstain"
            else:
                raw["properties"]["findings"]["items"]["oneOf"].reverse()
            return raw

        monkeypatch.setattr(binding, "response_schema", schema)
    with pytest.raises(ValueError):
        probe.prepare()


def test_mock_transport_receives_exact_bytes_and_stops_after_three_abstentions(tmp_path, prepared):
    plan, jobs, bodies = prepared
    seen = []

    def handle(request):
        assert request.content == bodies[jobs[len(seen)][0]]
        journal = tmp_path / f'run-{plan["execution_sha256"]}' / "journal.jsonl"
        assert json.loads(journal.read_text(encoding="utf-8").splitlines()[-1])["event"] == "reserved_before_dispatch"
        seen.append(request.content)
        return httpx.Response(200, json=payload())

    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        summary = execute(prepared, tmp_path, client)
        assert summary["status"] == "stopped" and summary["stop_reason"] == "binding_smoke_all_abstained"
        assert len(seen) == summary["provider_attempts"] == summary["abstention_findings"] == 3
        assert summary["assessment_findings"] == 0 and summary["validator_passed_responses"] == 3
        assert summary["uncertain_charge_reservation_usd"] == "0"
        assert summary["uncached_token_cost_upper_bound_usd"] == "0.000216"
        with pytest.raises(FileExistsError):
            execute(prepared, tmp_path, client)
    directory = tmp_path / f'run-{plan["execution_sha256"]}'
    assert len(seen) == 3
    assert all("private-test" not in path.read_text(encoding="utf-8") for path in directory.iterdir())
    assert summary["journal_sha256"] == runner.prep.digest((directory / "journal.jsonl").read_text(encoding="utf-8"))


def test_mock_assessment_is_bound_by_v2_and_not_a_semantic_success_metric(tmp_path, prepared):
    _, jobs, _ = prepared
    seen = []

    def handle(request):
        reply = assessment(jobs[0][1]) if not seen else None
        seen.append(request.content)
        return httpx.Response(200, json=payload(reply))

    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        summary = execute(prepared, tmp_path, client)
    assert len(seen) == 24 and summary["status"] == "complete"
    assert summary["assessment_findings"] == summary["responses_with_assessments"] == 1
    assert summary["abstention_findings"] == 23 and summary["semantic_accuracy_measured"] is False
    plan = prepared[0]
    events = [json.loads(line) for line in (tmp_path / f'run-{plan["execution_sha256"]}' / "journal.jsonl").read_text(encoding="utf-8").splitlines()]
    finding = events[1]["decision"]["assessments"][0]
    assert finding["target_ref"] is None  # Verified absence, not t_other.
    assert events[1]["decision"]["automatic_activation_allowed"] is False


def test_mock_smoke_findings_bind_absence_global_and_project_targets(tmp_path, prepared):
    plan, jobs, _ = prepared
    replies = [assessment(jobs[0][1]), assessment(jobs[1][1], stance="replacement"),
               assessment(jobs[2][1], stance="replacement", scope="atlas")]
    seen = []

    def handle(request):
        reply = replies[len(seen)] if len(seen) < 3 else None
        seen.append(request.content)
        return httpx.Response(200, json=payload(reply))

    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        summary = execute(prepared, tmp_path, client)
    assert summary["status"] == "complete" and summary["assessment_findings"] == 3
    events = [json.loads(line) for line in (tmp_path / f'run-{plan["execution_sha256"]}' / "journal.jsonl").read_text(encoding="utf-8").splitlines()]
    findings = [event["decision"]["assessments"][0] for event in events if event.get("decision", {}).get("assessments")]
    assert [(finding["scope_ref"], finding["target_ref"]) for finding in findings] == [
        ("global", None), ("global", "t_primary"), ("atlas", "t_project")]
    assert all(event["decision"]["automatic_activation_allowed"] is False for event in events if "decision" in event)


@pytest.mark.parametrize("change", ["validator", "mode", "early-stop", "order", "source", "policy-dependency", "partial", "context-seal", "wire-seal", "missing-wire", "wrong-context-type"])
def test_policy_or_binding_context_change_blocks_before_lock_and_dispatch(monkeypatch, tmp_path, prepared, change):
    plan, jobs, bodies = deepcopy(prepared)
    if change == "validator":
        plan["validator"] = "preference-semantic-admission-v1"
    elif change == "mode":
        plan["mode"] = "other"
    elif change == "early-stop":
        plan.pop("early_stop")
    elif change == "order":
        jobs[0], jobs[1] = jobs[1], jobs[0]
    elif change == "source":
        plan["source_sha256_utf8_lf"]["benchmarks/internal/preference_semantic_binding_v2.py"] = "f" * 64
    elif change == "policy-dependency":
        plan["source_sha256_utf8_lf"]["api/services/evidence_policy.py"] = "f" * 64
    elif change == "partial":
        key, shown, request = jobs[0]
        jobs[0] = key, shown.model_copy(update={"covered_identities": ()}), request
        plan["cases"][0]["context_sha256"] = runner.prep.digest(runner.prep.canonical(jobs[0][1].model_dump(mode="json")))
    elif change == "context-seal":
        plan["cases"][0].pop("context_sha256")
    elif change == "wire-seal":
        plan["cases"][0].pop("wire_sha256")
    elif change == "missing-wire":
        bodies = None
    else:
        key, shown, request = jobs[0]
        jobs[0] = key, shown.context, request
    reseal(plan)
    monkeypatch.setattr(runner, "fetch", lambda *args, **kwargs: pytest.fail("must not dispatch"))
    with pytest.raises(ValueError):
        execute((plan, jobs, bodies), tmp_path, None)
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("change", ["model-field", "forged-evidence", "wrong-shape"])
def test_invalid_v2_output_is_charged_once_and_not_repaired(monkeypatch, tmp_path, prepared, change):
    plan, jobs, _ = prepared
    reply = assessment(jobs[0][1])
    finding = reply["findings"][0]
    if change == "model-field":
        finding["target_ref"] = "t_other"
    elif change == "forged-evidence":
        finding["evidence"]["quote"] = "forged"
    else:
        reply = {"findings": []}
    calls = []
    monkeypatch.setattr(runner, "fetch", lambda *args, **kwargs: (calls.append(1), payload(reply))[1])
    summary = execute(prepared, tmp_path, None)
    assert len(calls) == 1 and summary["stop_reason"] == "frozen_validator_rejected"
    assert summary["uncached_token_cost_upper_bound_usd"] == "0.000072"
    assert summary["uncertain_charge_reservation_usd"] == "0" and summary["assessment_findings"] == 0
    event = json.loads((tmp_path / f'run-{plan["execution_sha256"]}' / "journal.jsonl").read_text(encoding="utf-8").splitlines()[-1])
    assert event["reply"] == reply and event["decision"]["status"] == "rejected"


def test_v2_wrapper_is_snapshotted_before_transport_can_mutate_it(monkeypatch, tmp_path, prepared):
    plan, jobs, bodies = deepcopy(prepared)
    seen = []

    def fetch(client, request, key, *, wire_body):
        # Simulate mutation of caller-owned lists/plan while a call is in flight.
        jobs.clear()
        plan["cases"].clear()
        seen.append(wire_body)
        return payload()

    monkeypatch.setattr(runner, "fetch", fetch)
    summary = execute((plan, jobs, bodies), tmp_path, None)
    assert len(seen) == 3 and summary["stop_reason"] == "binding_smoke_all_abstained"


def test_v2_timeout_retains_reservation_consumes_lock_and_never_retries(monkeypatch, tmp_path, prepared):
    calls = []

    def fetch(*args, **kwargs):
        calls.append(1)
        raise httpx.ReadTimeout("secret-bearing transport text")

    monkeypatch.setattr(runner, "fetch", fetch)
    summary = execute(prepared, tmp_path, None)
    assert len(calls) == 1 and summary["stop_reason"] == "ReadTimeout"
    assert summary["uncertain_charge_reservation_usd"] == "0.43"
    assert summary["uncached_token_cost_upper_bound_usd"] == "0"
    with pytest.raises(FileExistsError):
        execute(prepared, tmp_path, None)
    assert len(calls) == 1
    directory = tmp_path / f'run-{prepared[0]["execution_sha256"]}'
    assert "secret-bearing" not in (directory / "journal.jsonl").read_text(encoding="utf-8")


def test_v2_cap_retains_conservative_reservation_before_next_case(monkeypatch, tmp_path, prepared):
    response = payload(assessment(prepared[1][0][1]))
    response["usage"] = {"prompt_tokens": 1_000_000, "completion_tokens": 20, "total_tokens": 1_000_020}
    calls = []
    monkeypatch.setattr(runner, "fetch", lambda *args, **kwargs: (calls.append(1), response)[1])
    summary = execute(prepared, tmp_path, None)
    assert len(calls) == 1 and summary["stop_reason"] == "budget_reservation_unavailable"
    assert summary["cap_usd"] == "0.50" and summary["uncertain_charge_reservation_usd"] == "0"
    assert summary["uncached_token_cost_upper_bound_usd"] == "0.400032"


def test_cli_is_offline_and_has_no_execute_or_override_options(monkeypatch, capsys, prepared):
    monkeypatch.setattr(probe, "prepare", lambda: prepared)
    monkeypatch.setattr(httpx, "Client", lambda *args, **kwargs: pytest.fail("offline CLI must not create client"))
    monkeypatch.setattr("sys.argv", ["v2-preparation"])
    probe.main()
    assert json.loads(capsys.readouterr().out)["execution_sha256"] == prepared[0]["execution_sha256"]
    for option in ("--execute", "--approved-execution", "--model", "--case-id", "--cap", "--input-path", "--labels"):
        monkeypatch.setattr("sys.argv", ["v2-preparation", option])
        with pytest.raises(SystemExit) as exc:
            probe.main()
        assert exc.value.code == 2


def test_preparation_code_is_not_imported_by_production():
    for path in (runner.prep.ROOT / "api").rglob("*.py"):
        assert "preference_binding_development_v2" not in path.read_text(encoding="utf-8")
