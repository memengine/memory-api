"""Fixed six-request schema-order probe; offline preparation is the default."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import httpx

from benchmarks.internal import preference_abstention_diagnostic as diagnostic
from benchmarks.internal import preference_exploratory as prep
from benchmarks.internal import preference_exploratory_run as runner

CONDITIONS = ("sorted_control", "kind_first")
OUTPUT_ROOT = prep.ROOT / "artifacts/preference-schema-order-diagnostic"
CONSUMED_DIAGNOSTIC = "4fc07e038bf5f4a868239f36a719857ca2eede2f3cf91757c6f13ea5ba541974"


def prepare() -> tuple[dict, list, dict[str, bytes]]:
    # Reuse the fixed prompt/input preparation, not its consumed execution or journal.
    base, previous_jobs = diagnostic.prepare()
    controls = [(key, shown, request) for key, shown, request in previous_jobs if key.endswith("_strict_client")]
    if [key for key, _, _ in controls] != [f"{case_id}_strict_client" for case_id in diagnostic.CASE_IDS]:
        raise ValueError("fixed strict controls changed")
    jobs, rows, bodies = [], [], {}
    for previous_key, shown, request in controls:
        source_case_id = previous_key.removesuffix("_strict_client")
        control_text = prep.canonical(request)
        sorted_request = runner.parse_json(control_text)
        properties = sorted_request["response_format"]["json_schema"]["schema"]["$defs"]["Assessment"]["properties"]
        if list(properties) != ["evidence", "kind", "property_key", "scope_ref", "stance", "target_ref", "value"]:
            raise ValueError("assessment control ordering changed")
        for condition in CONDITIONS:
            wire_request = runner.parse_json(control_text)
            assessment = wire_request["response_format"]["json_schema"]["schema"]["$defs"]["Assessment"]
            if condition == "kind_first":
                fields = assessment["properties"]
                assessment["properties"] = {"kind": fields["kind"], **{key: value for key, value in fields.items() if key != "kind"}}
            # All other objects already have canonical ordering. Never sort again.
            text = json.dumps(wire_request, ensure_ascii=False, separators=(",", ":"))
            if prep.canonical(wire_request) != control_text:
                raise ValueError("order probe changed request meaning")
            key = f"{source_case_id}_{condition}"
            jobs.append((key, shown, request))
            bodies[key] = text.encode("utf-8")
            rows.append({
                "case_id": key, "source_case_id": source_case_id, "condition": condition,
                "action": "candidate_request", "request_sha256": prep.digest(control_text),
                "wire_sha256": prep.digest(text),
                "assessment_property_order": list(assessment["properties"]),
                "context_sha256": prep.digest(prep.canonical(shown.model_dump(mode="json"))),
            })
    plan = {
        "mode": "preference_schema_order_diagnostic_v1",
        "base_preflight_sha256": runner.APPROVED_BASE,
        "prompt_preparation_sha256": base["execution_sha256"],
        "source_sha256_utf8_lf": prep.digest(Path(__file__).read_text(encoding="utf-8")),
        "candidate": base["candidate"], "limits": base["limits"],
        "cap_usd": str(diagnostic.CAP), "per_attempt_reservation_usd": str(runner.RESERVATION),
        "case_count": 3, "request_count": 6, "conditions": list(CONDITIONS),
        "changed_factor": "Assessment.properties order: move kind first; leave every other byte unchanged",
        "schema_prompt_text_unchanged": True, "source_kind": "client_assertion",
        "consumed_authorizations_not_reused": [diagnostic.ORIGINAL_EXECUTION, CONSUMED_DIAGNOSTIC],
        "stop_on_any_frozen_validator_rejection": True,
        "semantic_accuracy_measured": False, "human_release_gate_met": False,
        "production_enablement_allowed": False, "holdouts_loaded": False, "ai_annotations_loaded": False,
        "cases": rows,
    }
    return {**plan, "execution_sha256": prep.digest(prep.canonical(plan))}, jobs, bodies


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--approved-execution")
    args = parser.parse_args()
    try:
        plan, jobs, bodies = prepare()
        if not args.execute:
            print(json.dumps(plan, indent=2))
            return
        if args.approved_execution != plan["execution_sha256"]:
            raise ValueError("explicit matching order-probe approval required")
        if (OUTPUT_ROOT / f"run-{args.approved_execution}").exists():
            raise ValueError("order-probe authorization already consumed")
        from api.settings import get_settings

        with httpx.Client(transport=httpx.HTTPTransport(retries=0), trust_env=False,
                          follow_redirects=False, timeout=30) as client:
            summary = runner.execute(plan, jobs, approved_digest=args.approved_execution,
                                     client=client, api_key=get_settings().openai_api_key,
                                     output_root=OUTPUT_ROOT, cap=diagnostic.CAP, wire_bodies=bodies)
        print(json.dumps(summary, indent=2))
        if summary["status"] != "complete":
            parser.exit(1, "Order probe stopped; authorization must not be rerun.\n")
    except (OSError, ValueError, UnicodeError) as exc:
        parser.exit(1, f"Order probe blocked: {type(exc).__name__}\n")


if __name__ == "__main__":
    main()
