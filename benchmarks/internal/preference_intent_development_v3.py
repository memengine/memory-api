"""Prepare sealed v3 development requests offline; no execution CLI or labels."""
from __future__ import annotations

import argparse
import json
from copy import deepcopy
from decimal import ROUND_CEILING, Decimal

from benchmarks.internal import preference_binding_development_v2 as previous
from benchmarks.internal import preference_exploratory as prep
from benchmarks.internal import preference_exploratory_run as runner
from benchmarks.internal import preference_semantic_intent_v3 as intent

INSTRUCTIONS = prep.ROOT / "benchmarks/internal/protocols/preference-intent-development-v3.instructions.txt"
OUTPUT_ROOT = prep.ROOT / "artifacts/preference-intent-development-v3"
CONSUMED = (*previous.CONSUMED, "a13e31dede0c1953b1834d8cdafdca0935138eda8988fdb56b8a9e3af795d3fd")


def provider_schema(shown) -> dict:
    """Narrow union adaptation; never remove evidence or value bounds."""
    schema = intent.response_schema(shown)
    items = schema["properties"]["findings"]["items"]
    if items.get("oneOf") != [{"$ref": "#/$defs/IntentFinding"}, {"$ref": "#/$defs/Abstention"}] or items.get("discriminator", {}).get("propertyName") != "kind":
        raise ValueError("v3 union changed")
    for name, kind in (("IntentFinding", "assessment"), ("Abstention", "abstain")):
        tag = schema["$defs"][name]["properties"]["kind"]
        if tag.get("const") != kind:
            raise ValueError("v3 kind tags changed")
        tag["enum"] = [tag.pop("const")]
    items["anyOf"] = items.pop("oneOf")
    items.pop("discriminator")
    return schema


def prepare() -> tuple[dict, list, dict[str, bytes]]:
    # Reuse fixed public fixture selection, candidate checks and frozen-asset
    # verification, NOT an old saved plan, reply, journal or authorization.
    base, old_jobs, _ = previous.prepare()
    instructions = INSTRUCTIONS.read_text(encoding="utf-8")
    jobs, rows, bodies, input_proxy = [], [], {}, 0
    for (_, shown, _), row in zip(old_jobs, base["cases"], strict=True):
        case_id = row["case_id"]
        schema = provider_schema(shown)
        fields = schema["$defs"]["IntentFinding"]["properties"]
        if list(fields) != ["kind", "property_key", "scope_ref", "intent", "evidence", "value"]:
            raise ValueError("intent wire ordering changed")
        projection = intent.model_input(shown)
        if projection is None:
            raise ValueError("fixed eligible source coverage changed")
        request = {"model": base["candidate"]["model"], "messages": [
            {"role": "system", "content": instructions},
            {"role": "user", "content": prep.canonical(projection)}],
            "response_format": {"type": "json_schema", "json_schema": {
                "name": "preference_semantic_intent_v3", "strict": True, "schema": schema}},
            "temperature": 0, "max_completion_tokens": 1024, "n": 1, "stream": False, "service_tier": "default"}
        body = json.dumps(request, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        bodies[case_id] = body
        jobs.append((case_id, shown, request))
        proxy = len(body) + 1024  # Byte/framing planning proxy, not billed tokens.
        input_proxy += proxy
        rows.append({**row, "request_sha256": prep.digest(prep.canonical(request)),
            "wire_sha256": prep.digest(body.decode("utf-8")), "interpretation_property_order": list(fields),
            "input_token_planning_proxy": proxy})
    cost = (input_proxy * runner.INPUT_RATE + len(jobs) * 1024 * runner.OUTPUT_RATE) / 1_000_000
    plan = {**{key: value for key, value in base.items() if key not in {"cases", "execution_sha256"}},
        "mode": "preference_intent_development_v3", "validator": runner.INTENT_VALIDATOR,
        "early_stop": deepcopy(runner.INTENT_EARLY_STOP),
        "instructions_sha256_utf8_lf": prep.digest(instructions),
        "source_sha256_utf8_lf": {path: prep.digest((prep.ROOT / path).read_text(encoding="utf-8"))
            for path in dict.fromkeys((*previous.SOURCE_PATHS, *runner.INTENT_VALIDATION_SOURCES))},
        "input_token_planning_proxy": input_proxy,
        "planning_cost_usd": str(cost.quantize(Decimal("0.01"), rounding=ROUND_CEILING)),
        "consumed_authorizations_not_reused": list(CONSUMED),
        "candidate_routes_are_write_permission": False,
        "preparation_is_not_provider_acceptance": True, "cases": rows}
    return {**plan, "execution_sha256": prep.digest(prep.canonical(plan))}, jobs, bodies


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="Offline only; also the default")
    parser.add_argument("--save", action="store_true", help="Create a new local preparation artifact, never overwrite")
    args = parser.parse_args()
    try:
        plan = prepare()[0]
        if args.save:
            OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)
            runner.durable_write(OUTPUT_ROOT / "prepared-v3.json", plan)
        print(json.dumps(plan, ensure_ascii=False, indent=2))
    except (OSError, ValueError, KeyError) as exc:
        parser.exit(1, f"V3 preparation blocked: {type(exc).__name__}\n")


if __name__ == "__main__":
    main()
