"""Offline preflight only. No model outputs or semantic accuracy oracle."""
import json
from copy import deepcopy
from pathlib import Path

import pytest

from benchmarks.internal import preference_exploratory as experiment
from benchmarks.internal.preference_semantic_contract import response_schema
from benchmarks.internal.preference_semantic_dataset import (
    DraftCase,
    context_for,
    load_development_pack,
)


@pytest.fixture(scope="module")
def materials():
    pack, _ = load_development_pack()
    protocol = json.loads(experiment.PROTOCOL.read_text(encoding="utf-8"))
    return pack, protocol


def shown(materials, index=0):
    pack, _ = materials
    return context_for(pack, DraftCase.model_validate(pack["cases"][index]))


def test_preflight_is_deterministic_and_never_grants_execution(monkeypatch):
    from api.services.llm_service import LLMService

    monkeypatch.setattr(LLMService, "__init__", lambda *args, **kwargs: pytest.fail("no provider service may be constructed"))
    plan = experiment.build_preflight()
    assert plan == experiment.build_preflight()
    assert (plan["case_count"], plan["candidate_calls"], plan["no_call_controls"]) == (360, 339, 21)
    assert plan["related_scenario_count"] == 120
    for flag in ("provider_execution_approved", "human_release_gate_met", "production_enablement_allowed",
                 "ai_annotations_loaded", "holdouts_loaded", "semantic_accuracy_measured",
                 "request_schema_provider_verified", "planning_cost_is_hard_spending_cap"):
        assert plan[flag] is False
    assert plan["model_calls"] == plan["actual_provider_cost_usd"] == 0
    assert plan["approved_spending_cap_usd"] is None
    assert plan["max_output_tokens_total"] == 339 * 1024
    assert plan["preflight_sha256"] == experiment.digest(experiment.canonical({key: value for key, value in plan.items() if key != "preflight_sha256"}))


def test_only_fixed_public_assets_are_read_not_ai_drafts_or_holdouts(monkeypatch):
    original_read = Path.read_text
    seen = []
    allowed = {
        experiment.PROTOCOL, experiment.INSTRUCTIONS, experiment.FREEZE,
        experiment.ROOT / "benchmarks/internal/datasets/preference_semantic/development/inputs-v1.json",
        experiment.ROOT / "benchmarks/internal/datasets/preference_semantic/development/manifest-v1.json",
        experiment.ROOT / "benchmarks/internal/preference_exploratory.py",
        *(experiment.ROOT / path for path in experiment.FROZEN_PATHS),
    }

    def read(path, *args, **kwargs):
        assert path in allowed, f"unexpected read: {path}"
        seen.append(path)
        return original_read(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", read)
    experiment.build_preflight()
    assert set(seen) == allowed


def test_request_has_only_projected_source_and_frozen_instructions(materials):
    pack, protocol = materials
    instructions = experiment.INSTRUCTIONS.read_text(encoding="utf-8")
    for raw in pack["cases"]:
        context = context_for(pack, DraftCase.model_validate(raw))
        request = experiment.build_request(context, instructions=instructions, candidate=protocol["candidate"])
        if request is None:
            continue
        assert set(request) == {"model", "messages", "response_format", "temperature", "max_completion_tokens", "n", "stream"}
        assert request["messages"][0] == {"role": "system", "content": instructions}
        source = json.loads(request["messages"][1]["content"])
        assert set(source) == {"properties", "scopes", "existing_targets", "turns"}
        assert all(set(target) == {"ref", "property_key", "scope_ref", "value", "choice_state"} for target in source["existing_targets"])
        assert all(set(turn) == {"turn_index", "role", "content", "source_kind"} for turn in source["turns"])
        assert "case_id" not in source and "annotations" not in source
        assert request["response_format"]["json_schema"]["strict"] is True


def test_blocked_principal_and_ineligible_source_have_no_request(materials):
    context = shown(materials)
    blocked = context.model_copy(update={"principal": context.principal.model_copy(update={"blocked": True})})
    candidate = materials[1]["candidate"]
    assert experiment.build_request(blocked, instructions="frozen", candidate=candidate) is None
    tool_only = context.model_copy(update={"turns": (context.turns[0].model_copy(update={"role": "tool", "source_kind": "tool_result"}),)})
    assert experiment.build_request(tool_only, instructions="frozen", candidate=candidate) is None


def test_schema_adapter_preserves_all_constraints_and_original(materials):
    context = shown(materials)
    original = response_schema(context)
    before = deepcopy(original)
    adapted = experiment.provider_schema(context)
    assert response_schema(context) == before
    expected = deepcopy(original)
    items = expected["properties"]["findings"]["items"]
    items["anyOf"] = items.pop("oneOf")
    items.pop("discriminator")
    for name in ("Assessment", "Abstention"):
        tag = expected["$defs"][name]["properties"]["kind"]
        tag["enum"] = [tag.pop("const")]
    assert adapted == expected
    assert adapted["properties"]["findings"]["minItems"] == 1
    assert adapted["properties"]["findings"]["maxItems"] == 16
    for name in ("Assessment", "Abstention", "EvidenceQuote"):
        obj = adapted["$defs"][name]
        assert obj["additionalProperties"] is False
        assert set(obj["required"]) == set(obj["properties"])


@pytest.mark.parametrize("change", ["overlap", "unexpected_union"])
def test_schema_adapter_refuses_unexpected_branch_changes(materials, monkeypatch, change):
    schema = response_schema(shown(materials))
    if change == "overlap":
        schema["$defs"]["Abstention"]["properties"]["kind"]["const"] = "assessment"
    else:
        schema["properties"]["findings"]["items"]["oneOf"].append({"type": "object"})
    monkeypatch.setattr(experiment, "response_schema", lambda context: schema)
    with pytest.raises(ValueError):
        experiment.provider_schema(shown(materials))


@pytest.mark.parametrize("change", ["approval", "hash", "model", "rate", "tokens", "bool_tokens", "bool_temperature", "count", "amendment", "endpoint", "retry"])
def test_preflight_rejects_unapproved_protocol_drift(materials, monkeypatch, change):
    protocol = deepcopy(materials[1])
    if change == "approval":
        protocol["provider_execution_approved"] = True
    elif change == "hash":
        protocol["inputs_sha256_utf8_lf"] = "f" * 64
    elif change == "model":
        protocol["candidate"]["model"] = "gpt-4.1-mini"
    elif change == "rate":
        protocol["candidate"]["input_usd_per_million_tokens"] = "0.01"
    elif change == "tokens":
        protocol["candidate"]["max_completion_tokens"] = 2048
    elif change == "bool_tokens":
        protocol["candidate"]["max_completion_tokens"] = True
    elif change == "bool_temperature":
        protocol["candidate"]["temperature"] = False
    elif change == "count":
        protocol["candidate_call_count"] = 360
    elif change == "endpoint":
        protocol["candidate"]["endpoint"] = "https://example.invalid/chat/completions"
    elif change == "retry":
        protocol["future_execution_limits"]["sdk_retries"] = 2
    else:
        protocol["does_not_amend"] = "human-review-no-longer-required"
    read = Path.read_text
    monkeypatch.setattr(Path, "read_text", lambda path, *args, **kwargs: json.dumps(protocol) if path == experiment.PROTOCOL else read(path, *args, **kwargs))
    with pytest.raises(ValueError):
        experiment.build_preflight()


def test_frozen_asset_drift_fails_before_request_preparation(monkeypatch):
    path = experiment.ROOT / "benchmarks/internal/preference_semantic_contract.py"
    read = Path.read_text
    monkeypatch.setattr(Path, "read_text", lambda current, *args, **kwargs: "changed" if current == path else read(current, *args, **kwargs))
    monkeypatch.setattr(experiment, "build_request", lambda *args, **kwargs: pytest.fail("must verify frozen assets first"))
    with pytest.raises(ValueError, match="frozen asset changed"):
        experiment.build_preflight()


def test_cli_has_no_execute_input_or_label_options(monkeypatch, capsys):
    monkeypatch.setattr("sys.argv", ["preflight", "--dry-run"])
    monkeypatch.setattr(experiment, "build_preflight", lambda: {"model_calls": 0, "provider_execution_approved": False})
    experiment.main()
    assert json.loads(capsys.readouterr().out)["model_calls"] == 0
    for args in (["--execute"], ["--input", "private/holdout.json"], ["--labels", "draft.json"]):
        monkeypatch.setattr("sys.argv", ["preflight", *args])
        with pytest.raises(SystemExit) as exc:
            experiment.main()
        assert exc.value.code == 2
