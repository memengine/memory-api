"""Fixed nine-call diagnostic, separate from the consumed exploratory run."""
from __future__ import annotations

import argparse
import json
from decimal import Decimal

import httpx

from benchmarks.internal import preference_exploratory as prep
from benchmarks.internal import preference_exploratory_run as runner
from benchmarks.internal.preference_semantic_dataset import (
    DraftCase,
    context_for,
    load_development_pack,
)

CASE_IDS = ("d0001", "d0121", "d0151")  # Adoption, committed replacement, scoped exception.
CONDITIONS = ("strict_client", "json_client", "strict_direct")
CAP = Decimal("0.50")
OUTPUT_ROOT = prep.ROOT / "artifacts/preference-abstention-diagnostic"
ORIGINAL_EXECUTION = "41f3b1caaa1e9799872b4608092c1466833eb85b4364af49c9aa8d0f1dcbb48e"
ORIGINAL_JOURNAL = "569e3b35b46eabea002cd732256a086015589121d0588da81b554e7bf5155a49"
SCHEMA_PREFIX = "\nReturn JSON using this output contract, also supplied as text in every condition:\n"


def prepare() -> tuple[dict, list]:
    base = prep.build_preflight()
    if base["preflight_sha256"] != runner.APPROVED_BASE:
        raise ValueError("base instructions, inputs or schema adapter changed")
    pack, _ = load_development_pack()
    cases = {raw["case_id"]: raw for raw in pack["cases"]}
    instructions = prep.INSTRUCTIONS.read_text(encoding="utf-8")
    jobs, rows = [], []
    for case_id in CASE_IDS:
        case = DraftCase.model_validate(cases[case_id])
        original = context_for(pack, case)
        if any(turn.source_kind != "client_assertion" for turn in original.turns if turn.role == "user"):
            raise ValueError("source-kind control changed")
        shared_instructions = instructions + SCHEMA_PREFIX + prep.canonical(prep.provider_schema(original))
        for condition in CONDITIONS:
            shown = original
            if condition == "strict_direct":
                # Synthetic source-kind contrast only: authority/identity unchanged.
                shown = original.model_copy(update={"turns": tuple(
                    turn.model_copy(update={"source_kind": "direct_user_input"}) if turn.role == "user" else turn
                    for turn in original.turns
                )})
            request = prep.build_request(shown, instructions=shared_instructions, candidate=base["candidate"])
            if request is None:
                raise ValueError("fixed diagnostic must have eligible user input")
            request = {**request, "service_tier": "default"}
            if condition == "json_client":
                request["response_format"] = {"type": "json_object"}
            key = f"{case_id}_{condition}"
            jobs.append((key, shown, request))
            rows.append({"case_id": key, "source_case_id": case_id, "condition": condition,
                         "action": "candidate_request", "request_sha256": prep.digest(prep.canonical(request)),
                         "context_sha256": prep.digest(prep.canonical(shown.model_dump(mode="json")))})
    if len(jobs) != 9:
        raise ValueError("diagnostic coverage changed")
    plan = {
        "mode": "preference_abstention_diagnostic_v1", "base_preflight_sha256": runner.APPROVED_BASE,
        "candidate": base["candidate"], "limits": base["future_execution_limits"],
        "cap_usd": str(CAP), "per_attempt_reservation_usd": str(runner.RESERVATION),
        "case_count": 3, "request_count": 9, "conditions": list(CONDITIONS),
        "base_instructions_sha256": base["instructions_sha256_utf8_lf"],
        "shared_schema_prompt_prefix": SCHEMA_PREFIX,
        "shared_schema_prompt_is_change_from_original_run": True,
        "source_kind_change_is_synthetic_only": True,
        "original_interrupted_execution_sha256": ORIGINAL_EXECUTION,
        "original_interrupted_journal_sha256": ORIGINAL_JOURNAL,
        "source_sha256_utf8_lf": {
            path: prep.digest((prep.ROOT / path).read_text(encoding="utf-8")) for path in (
                "benchmarks/internal/preference_abstention_diagnostic.py",
                "benchmarks/internal/preference_exploratory_run.py",
            )
        },
        "semantic_accuracy_measured": False, "human_release_gate_met": False,
        "production_enablement_allowed": False, "holdouts_loaded": False, "ai_annotations_loaded": False,
        "cases": rows,
    }
    return {**plan, "execution_sha256": prep.digest(prep.canonical(plan))}, jobs


def interrupted_report() -> dict:
    """Correct derived reporting; never replace the original journal/manifest."""
    run = runner.OUTPUT_ROOT / f"run-{ORIGINAL_EXECUTION}"
    text = (run / "journal.jsonl").read_text(encoding="utf-8")
    if prep.digest(text) != ORIGINAL_JOURNAL:
        raise ValueError("interrupted journal changed; re-inspect before any diagnostic")
    events = [runner.parse_json(line) for line in text.splitlines()]
    responses = [event for event in events if event["event"] == "response"]
    valid = [event for event in responses if event.get("decision", {}).get("status") == "attributed_proposals_only"]
    completed = {event["case_id"] for event in responses}
    unmatched = [event["case_id"] for event in events
                 if event["event"] == "reserved_before_dispatch" and event["case_id"] not in completed]
    return {
        "original_execution_sha256": ORIGINAL_EXECUTION, "original_journal_sha256": ORIGINAL_JOURNAL,
        "status": "interrupted_not_complete", "saved_responses": len(responses),
        "validator_passed_responses": len(valid),
        "responses_with_assessments": sum(bool(event["decision"]["assessments"]) for event in valid),
        "assessment_findings": sum(len(event["decision"]["assessments"]) for event in valid),
        "abstention_findings": sum(len(event["decision"]["abstentions"]) for event in valid),
        "requests_without_saved_response": unmatched,
        "uncached_token_cost_upper_bound_usd": str(sum((Decimal(event.get(
            "uncached_token_cost_upper_bound_usd", "0")) for event in responses), Decimal(0))),
        "unsettled_reservation_upper_bound_usd": str(len(unmatched) * runner.RESERVATION),
        "semantic_accuracy_measured": False, "human_release_gate_met": False,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--approved-execution")
    args = parser.parse_args()
    try:
        plan, jobs = prepare()
        historical = interrupted_report()
        if not args.execute:
            print(json.dumps({"plan": plan, "interrupted_report": historical}, indent=2))
            return
        if args.approved_execution != plan["execution_sha256"]:
            raise ValueError("explicit matching diagnostic approval required")
        if (OUTPUT_ROOT / f"run-{args.approved_execution}").exists():
            raise ValueError("diagnostic authorization already consumed")
        from api.settings import get_settings

        with httpx.Client(transport=httpx.HTTPTransport(retries=0), trust_env=False,
                          follow_redirects=False, timeout=30) as client:
            summary = runner.execute(plan, jobs, approved_digest=args.approved_execution,
                                     client=client, api_key=get_settings().openai_api_key,
                                     output_root=OUTPUT_ROOT, cap=CAP)
        print(json.dumps(summary, indent=2))
        if summary["status"] != "complete":
            parser.exit(1, "Diagnostic stopped; authorization must not be rerun.\n")
    except (OSError, ValueError) as exc:
        parser.exit(1, f"Diagnostic blocked: {type(exc).__name__}\n")


if __name__ == "__main__":
    main()
