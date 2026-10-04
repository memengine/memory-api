"""Prepare 24 fixed v2 development requests offline. No execution CLI or labels."""
from __future__ import annotations

import argparse
import json
from decimal import ROUND_CEILING, Decimal

from benchmarks.internal import preference_exploratory as prep
from benchmarks.internal import preference_exploratory_run as runner
from benchmarks.internal import preference_semantic_binding_v2 as binding
from benchmarks.internal.preference_semantic_dataset import (
    DraftCase,
    context_for,
    load_development_pack,
)

FREEZE = prep.ROOT / "benchmarks/internal/protocols/preference-semantic-binding-v2.freeze.json"
INSTRUCTIONS = prep.ROOT / "benchmarks/internal/protocols/preference-binding-development-v2.instructions.txt"
OUTPUT_ROOT = prep.ROOT / "artifacts/preference-binding-development-v2"
CAP = Decimal("0.50")
# Eight familiar development groups, each with en/hi/hinglish variants. These
# author tags select coverage, never provide answers or independent labels.
GROUP_STARTS = (1, 31, 91, 121, 151, 181, 241, 271)
CASE_IDS = runner.BINDING_EARLY_CASE_IDS + tuple(
    f"d{start + offset:04d}" for start in GROUP_STARTS for offset in range(3)
    if f"d{start + offset:04d}" not in runner.BINDING_EARLY_CASE_IDS
)
FROZEN_PATHS = {*prep.FROZEN_PATHS, "benchmarks/internal/preference_semantic_binding_v2.py",
                "benchmarks/internal/protocols/preference-semantic-binding-v2.json", "docs/preference_semantic_binding_v2.md"}
SOURCE_PATHS = tuple(dict.fromkeys(("benchmarks/internal/preference_binding_development_v2.py",
    "benchmarks/internal/preference_exploratory.py", "benchmarks/internal/preference_semantic_dataset.py",
    *runner.BINDING_VALIDATION_SOURCES)))
CONSUMED = (
    "41f3b1caaa1e9799872b4608092c1466833eb85b4364af49c9aa8d0f1dcbb48e",
    "4fc07e038bf5f4a868239f36a719857ca2eede2f3cf91757c6f13ea5ba541974",
    "696dcb161664c8673cc3c1d82f151e2a2b82de0dc7e5ae467e4cebe5603717dd",
)


def provider_schema(shown: binding.BindingSnapshot) -> dict:
    """Only adapt the closed kind-tagged union; preserve every other constraint."""
    schema = binding.response_schema(shown)
    items = schema["properties"]["findings"]["items"]
    if items.get("oneOf") != [{"$ref": "#/$defs/Interpretation"}, {"$ref": "#/$defs/Abstention"}] or items.get("discriminator", {}).get("propertyName") != "kind":
        raise ValueError("v2 union changed")
    for name, kind in (("Interpretation", "assessment"), ("Abstention", "abstain")):
        tag = schema["$defs"][name]["properties"]["kind"]
        if tag.get("const") != kind:
            raise ValueError("v2 kind tags changed")
        tag["enum"] = [tag.pop("const")]
    items["anyOf"] = items.pop("oneOf")
    items.pop("discriminator")
    return schema


def prepare() -> tuple[dict, list, dict[str, bytes]]:
    frozen = runner.parse_json(FREEZE.read_text(encoding="utf-8"))
    if frozen.get("protocol_id") != runner.BINDING_VALIDATOR or frozen.get("hash_encoding") != "utf-8-lf" or set(frozen["sha256"]) != FROZEN_PATHS:
        raise ValueError("unexpected v2 freeze")
    for path, expected in frozen["sha256"].items():
        if prep.digest((prep.ROOT / path).read_text(encoding="utf-8")) != expected:
            raise ValueError("frozen contract asset changed")
    # Same pinned candidate and limits, not the consumed v1 execution plan.
    protocol_text = prep.PROTOCOL.read_text(encoding="utf-8")
    protocol = runner.parse_json(protocol_text)
    candidate = protocol["candidate"]
    if (candidate["provider"], candidate["model"], candidate["endpoint"], candidate["temperature"],
        candidate["max_completion_tokens"], candidate["input_usd_per_million_tokens"], candidate["output_usd_per_million_tokens"]) != (
        "openai", "gpt-4.1-mini-2025-04-14", "https://api.openai.com/v1/chat/completions", 0, 1024, "0.40", "1.60"):
        raise ValueError("candidate needs a new model/pricing review")
    if type(candidate["temperature"]) is not int or type(candidate["max_completion_tokens"]) is not int:
        raise ValueError("candidate numeric bounds changed")
    limits = {"attempts_per_eligible_case": 1, "sdk_retries": 0, "concurrency": 1,
              "request_timeout_seconds": 30, "fallback_models": False,
              "automatic_reruns": False, "stop_on_transport_or_output_failure": True}
    if protocol["future_execution_limits"] != limits or runner.RESERVATION != Decimal("0.43"):
        raise ValueError("execution bounds changed")
    pack, _ = load_development_pack()  # Sealed public, unlabelled inputs only.
    cases = {raw["case_id"]: raw for raw in pack["cases"]}
    instructions = INSTRUCTIONS.read_text(encoding="utf-8")
    jobs, rows, bodies, input_proxy = [], [], {}, 0
    for case_id in CASE_IDS:
        case = DraftCase.model_validate(cases[case_id])
        context = context_for(pack, case)
        # These full synthetic fixtures explicitly cover their entire small
        # catalog, including absence. This is NOT a SQL completeness proof.
        shown = binding.BindingSnapshot(context=context, covered_identities=[
            {"property_key": prop.key, "scope_ref": scope.ref} for prop in context.properties for scope in context.scopes
        ])
        source = binding.model_input(shown)
        if source is None or any(turn.source_kind != "client_assertion" for turn in context.turns if turn.role == "user"):
            raise ValueError("fixed eligible source coverage changed")
        schema = provider_schema(shown)
        fields = schema["$defs"]["Interpretation"]["properties"]
        if list(fields) != ["kind", "property_key", "scope_ref", "stance", "evidence", "value"]:
            raise ValueError("interpretation wire ordering changed")
        request = {"model": candidate["model"], "messages": [
            {"role": "system", "content": instructions}, {"role": "user", "content": prep.canonical(source)}],
            "response_format": {"type": "json_schema", "json_schema": {
                "name": "preference_semantic_binding_v2", "strict": True, "schema": schema}},
            "temperature": 0, "max_completion_tokens": 1024, "n": 1, "stream": False, "service_tier": "default"}
        # Preserve the emitted schema order. Canonical hashing cannot seal order.
        body = json.dumps(request, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        bodies[case_id] = body
        jobs.append((case_id, shown, request))
        proxy = len(body) + 1024  # Planning proxy only, never token billing proof.
        input_proxy += proxy
        rows.append({"case_id": case_id, "language": case.language, "scenario_group": case.scenario_group,
                     "family": case.family, "action": "candidate_request",
                     "request_sha256": prep.digest(prep.canonical(request)), "wire_sha256": prep.digest(body.decode("utf-8")),
                     "context_sha256": prep.digest(prep.canonical(shown.model_dump(mode="json"))),
                     "interpretation_property_order": list(fields), "input_token_planning_proxy": proxy})
    cost = (input_proxy * runner.INPUT_RATE + len(jobs) * 1024 * runner.OUTPUT_RATE) / 1_000_000
    plan = {
        "mode": "preference_binding_development_v2", "validator": runner.BINDING_VALIDATOR,
        "early_stop": {"first_case_ids": list(runner.BINDING_EARLY_CASE_IDS), "stop_if_all_have_zero_assessments": True},
        "candidate": candidate, "limits": limits, "cap_usd": str(CAP), "per_attempt_reservation_usd": str(runner.RESERVATION),
        "case_count": 24, "request_count": 24, "related_scenario_count": 8, "language_counts": {"en": 8, "hi": 8, "hinglish": 8},
        "inputs_sha256_utf8_lf": protocol["inputs_sha256_utf8_lf"], "candidate_protocol_sha256_utf8_lf": prep.digest(protocol_text),
        "instructions_sha256_utf8_lf": prep.digest(instructions), "frozen_contract_assets_sha256": frozen["sha256"],
        "source_sha256_utf8_lf": {path: prep.digest((prep.ROOT / path).read_text(encoding="utf-8")) for path in SOURCE_PATHS},
        "input_token_planning_proxy": input_proxy, "planning_cost_usd": str(cost.quantize(Decimal("0.01"), rounding=ROUND_CEILING)),
        "planning_cost_is_hard_spending_cap": False, "first_request_is_schema_smoke": True,
        "stop_on_any_frozen_validator_rejection": True, "consumed_authorizations_not_reused": list(CONSUMED),
        "provider_execution_approved": False, "request_schema_provider_verified": False,
        "semantic_accuracy_measured": False, "human_release_gate_met": False, "production_enablement_allowed": False,
        "holdouts_loaded": False, "ai_annotations_loaded": False, "sql_completeness_verified": False, "sql_concurrency_verified": False,
        "model_calls": 0, "actual_provider_cost_usd": 0, "cases": rows,
    }
    return {**plan, "execution_sha256": prep.digest(prep.canonical(plan))}, jobs, bodies


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="Offline preparation only; also the default")
    parser.parse_args()
    try:
        print(json.dumps(prepare()[0], ensure_ascii=False, indent=2))
    except (OSError, ValueError, KeyError) as exc:
        parser.exit(1, f"V2 preparation blocked: {type(exc).__name__}\n")


if __name__ == "__main__":
    main()
