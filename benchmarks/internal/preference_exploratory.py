"""Read-only exploratory preflight. No provider client, execution or label reader."""
from __future__ import annotations

import argparse
import hashlib
import json
from decimal import ROUND_CEILING, Decimal

from benchmarks.internal.preference_semantic_contract import (
    ContractContext,
    model_input,
    response_schema,
)
from benchmarks.internal.preference_semantic_dataset import (
    ROOT,
    DraftCase,
    context_for,
    load_development_pack,
)

PROTOCOL = ROOT / "benchmarks/internal/protocols/preference-exploratory-v1.json"
INSTRUCTIONS = ROOT / "benchmarks/internal/protocols/preference-exploratory-v1.instructions.txt"
FREEZE = ROOT / "benchmarks/internal/protocols/preference-semantic-admission-v1.freeze.json"
FROZEN_PATHS = {
    "benchmarks/internal/preference_semantic_contract.py",
    "benchmarks/internal/protocols/preference-semantic-admission-v1.json",
    "docs/preference_semantic_admission_contract.md",
}


def canonical(value: dict) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def provider_schema(shown: ContractContext) -> dict:
    """Narrow adapter for the frozen, mutually exclusive kind-tagged union.

    Keep all bounds/references. Provider acceptance still requires a live check.
    Responses must still pass the unchanged frozen validator, not this adapter.
    """
    schema = response_schema(shown)
    items = schema["properties"]["findings"]["items"]
    refs = [{"$ref": "#/$defs/Assessment"}, {"$ref": "#/$defs/Abstention"}]
    if items.get("oneOf") != refs or items.get("discriminator", {}).get("propertyName") != "kind":
        raise ValueError("frozen union changed; do not silently adapt a different schema")
    for name, kind in (("Assessment", "assessment"), ("Abstention", "abstain")):
        tag = schema["$defs"][name]["properties"]["kind"]
        if tag.get("const") != kind:
            raise ValueError("union branches must have distinct fixed kind tags")
        tag["enum"] = [tag.pop("const")]
    items["anyOf"] = items.pop("oneOf")
    items.pop("discriminator")
    return schema


def build_request(shown: ContractContext, *, instructions: str, candidate: dict) -> dict | None:
    source = model_input(shown)
    if source is None:
        return None
    return {
        "model": candidate["model"],
        "messages": [{"role": "system", "content": instructions},
                     {"role": "user", "content": canonical(source)}],
        "response_format": {"type": "json_schema", "json_schema": {
            "name": "preference_semantic_reply", "strict": True, "schema": provider_schema(shown),
        }},
        "temperature": candidate["temperature"],
        "max_completion_tokens": candidate["max_completion_tokens"],
        "n": 1, "stream": False,
    }


def build_preflight() -> dict:
    pack, inspection = load_development_pack()  # Fixed public paths only.
    frozen = json.loads(FREEZE.read_text(encoding="utf-8"))
    if set(frozen["sha256"]) != FROZEN_PATHS:
        raise ValueError("unexpected frozen asset paths")
    for path, expected in frozen["sha256"].items():
        if digest((ROOT / path).read_text(encoding="utf-8")) != expected:
            raise ValueError(f"frozen asset changed: {path}")
    protocol_text = PROTOCOL.read_text(encoding="utf-8")
    protocol = json.loads(protocol_text)
    if protocol["protocol_id"] != "preference-exploratory-v1" or protocol["status"] != "dry-run-preparation-only":
        raise ValueError("only the exploratory preparation protocol is supported")
    for flag in ("provider_execution_approved", "holdout_execution_approved", "production_wiring_allowed", "human_release_gate_met"):
        if protocol[flag] is not False:
            raise ValueError("preflight flags cannot grant execution or release authority")
    if protocol["does_not_amend"] != frozen["protocol_id"]:
        raise ValueError("exploration must not amend the human-review release protocol")
    inputs_text = (ROOT / "benchmarks/internal/datasets/preference_semantic/development/inputs-v1.json").read_text(encoding="utf-8")
    if digest(inputs_text) != protocol["inputs_sha256_utf8_lf"]:
        raise ValueError("exploratory input hash mismatch")
    instructions = INSTRUCTIONS.read_text(encoding="utf-8")
    candidate = protocol["candidate"]
    if candidate["provider"] != "openai" or candidate["model"] != "gpt-4.1-mini-2025-04-14":
        raise ValueError("changed candidate needs a new model/pricing preflight")
    if candidate["endpoint"] != "https://api.openai.com/v1/chat/completions":
        raise ValueError("unexpected provider endpoint")
    if type(candidate["temperature"]) is not int or candidate["temperature"] != 0 or type(candidate["max_completion_tokens"]) is not int or not 1 <= candidate["max_completion_tokens"] <= 1024:
        raise ValueError("candidate output bounds changed")
    if protocol["future_execution_limits"] != {
        "attempts_per_eligible_case": 1, "sdk_retries": 0, "concurrency": 1,
        "request_timeout_seconds": 30, "fallback_models": False,
        "automatic_reruns": False, "stop_on_transport_or_output_failure": True,
    }:
        raise ValueError("single-run failure policy changed")
    rows, input_proxy = [], 0
    for raw in sorted(pack["cases"], key=lambda item: item["case_id"]):
        case = DraftCase.model_validate(raw)
        request = build_request(context_for(pack, case), instructions=instructions, candidate=candidate)
        if request is None:
            rows.append({"case_id": case.case_id, "action": "no_call"})
            continue
        # Deliberately pessimistic planning proxy, not a tokenizer/billing proof.
        # Includes the entire request JSON (even non-prompt fields) and padding.
        proxy = len(canonical(request).encode("utf-8")) + 1024
        input_proxy += proxy
        rows.append({"case_id": case.case_id, "action": "candidate_request",
                     "request_sha256": digest(canonical(request)),
                     "response_schema_sha256": digest(canonical(request["response_format"]["json_schema"]["schema"])),
                     "input_token_planning_proxy": proxy})
    calls = sum(row["action"] == "candidate_request" for row in rows)
    if (len(rows), calls, len(rows) - calls) != (protocol["case_count"], protocol["candidate_call_count"], protocol["candidate_no_call_count"]):
        raise ValueError("candidate case/call coverage changed")
    if (candidate["input_usd_per_million_tokens"], candidate["output_usd_per_million_tokens"]) != ("0.40", "1.60"):
        raise ValueError("candidate pricing changed; reverify before a new preflight")
    input_rate, output_rate = Decimal("0.40"), Decimal("1.60")
    output_bound = calls * candidate["max_completion_tokens"]
    estimate = (input_proxy * input_rate + output_bound * output_rate) / 1_000_000
    plan = {
        "protocol_id": protocol["protocol_id"], "mode": "dry_run_exploratory_only",
        "protocol_sha256_utf8_lf": digest(protocol_text),
        "instructions_sha256_utf8_lf": digest(instructions),
        "preflight_code_sha256_utf8_lf": digest((ROOT / "benchmarks/internal/preference_exploratory.py").read_text(encoding="utf-8")),
        "frozen_contract_assets_sha256": frozen["sha256"],
        "inputs_sha256_utf8_lf": protocol["inputs_sha256_utf8_lf"],
        "candidate": candidate, "future_execution_limits": protocol["future_execution_limits"],
        "case_count": len(rows), "candidate_calls": calls, "no_call_controls": len(rows) - calls,
        "related_scenario_count": inspection["related_scenario_count"],
        "input_token_planning_proxy": input_proxy, "max_output_tokens_total": output_bound,
        "planning_cost_usd": str(estimate.quantize(Decimal("0.01"), rounding=ROUND_CEILING)),
        "planning_cost_is_hard_spending_cap": False, "approved_spending_cap_usd": None,
        "request_schema_provider_verified": False, "provider_execution_approved": False,
        "human_release_gate_met": False, "production_enablement_allowed": False,
        "ai_annotations_loaded": False, "holdouts_loaded": False,
        "model_calls": 0, "actual_provider_cost_usd": 0,
        "semantic_accuracy_measured": False, "cases": rows,
    }
    return {**plan, "preflight_sha256": digest(canonical(plan))}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="Preflight only; also the default. No execute option exists.")
    parser.parse_args()
    try:
        print(json.dumps(build_preflight(), ensure_ascii=False, indent=2))
    except (OSError, ValueError, KeyError) as exc:
        parser.exit(1, f"Invalid exploratory preflight: {exc}\n")


if __name__ == "__main__":
    main()
