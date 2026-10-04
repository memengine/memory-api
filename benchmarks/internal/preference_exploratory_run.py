"""One-shot synthetic development executor; no retries, labels or memory writes."""
from __future__ import annotations

import argparse
import json
import os
import time
from copy import deepcopy
from datetime import UTC, datetime
from decimal import ROUND_CEILING, Decimal
from pathlib import Path

import httpx

from benchmarks.internal import preference_exploratory as prep
from benchmarks.internal.preference_semantic_contract import validate_reply
from benchmarks.internal.preference_semantic_dataset import (
    DraftCase,
    context_for,
    load_development_pack,
)

APPROVED_BASE = "83345867ef8452189b04d3bf9c979dbd51382bbe82aff0a82ffae7e5817f2150"
CAP = Decimal("2.00")
# Published model context ceiling, NOT the preflight's estimated byte proxy.
MAX_PROMPT_TOKENS = 1_047_576
MAX_OUTPUT_TOKENS = 1024
INPUT_RATE, OUTPUT_RATE = Decimal("0.40"), Decimal("1.60")
RESERVATION = ((MAX_PROMPT_TOKENS * INPUT_RATE + MAX_OUTPUT_TOKENS * OUTPUT_RATE)
               / 1_000_000).quantize(Decimal("0.01"), rounding=ROUND_CEILING)
MAX_RESPONSE_BYTES = 1_048_576
OUTPUT_ROOT = prep.ROOT / "artifacts/preference-exploratory"
BINDING_VALIDATOR = "preference-semantic-binding-v2"
BINDING_EARLY_CASE_IDS = ("d0001", "d0121", "d0151")
BINDING_VALIDATION_SOURCES = (
    "benchmarks/internal/preference_exploratory_run.py",
    "benchmarks/internal/preference_semantic_binding_v2.py",
    "benchmarks/internal/preference_semantic_contract.py",
    "api/schemas/preferences.py", "api/services/evidence_policy.py", "api/services/extraction_service.py",
)
INTENT_VALIDATOR = "preference-semantic-intent-v3"
INTENT_EARLY_STOP = {"first_case_ids": list(BINDING_EARLY_CASE_IDS),
                     "stop_if_all_have_zero_usable_candidates": True}
INTENT_VALIDATION_SOURCES = tuple(dict.fromkeys((*BINDING_VALIDATION_SOURCES,
    "benchmarks/internal/preference_semantic_intent_v3.py",
    "benchmarks/internal/preference_intent_development_v3.py",
    "benchmarks/internal/preference_binding_development_v2.py",
    "benchmarks/internal/preference_exploratory.py",
    "benchmarks/internal/preference_semantic_dataset.py",
    "benchmarks/internal/protocols/preference-intent-development-v3.instructions.txt",
    "benchmarks/internal/protocols/preference-exploratory-v1.json",
    "benchmarks/internal/protocols/preference-semantic-binding-v2.freeze.json",
    "benchmarks/internal/protocols/preference-semantic-admission-v1.json",
    "benchmarks/internal/protocols/preference-semantic-binding-v2.json",
    "docs/preference_semantic_admission_contract.md", "docs/preference_semantic_binding_v2.md",
    "benchmarks/internal/datasets/preference_semantic/development/inputs-v1.json",
    "benchmarks/internal/datasets/preference_semantic/development/manifest-v1.json",
    "docs/preference_semantic_intent_v3.md", "docs/preference_intent_development_v3.md",
)))


def validation_policy(plan: dict, jobs: list):
    """Fixed offline validator selection, never a caller-supplied callback.

    Old plans retain v1. V2/v3 seal wrapper contexts, source and stop policy;
    the caller still needs separate approval of the complete execution digest.
    """
    selection = plan.get("validator", "preference-semantic-admission-v1")
    if selection == "preference-semantic-admission-v1":
        if plan.get("mode") in {"preference_binding_development_v2", "preference_intent_development_v3"} or "early_stop" in plan:
            raise ValueError("binding plan requires explicit validation")
        if any(not isinstance(shown, prep.ContractContext) for _, shown, _ in jobs):
            raise ValueError("unexpected v1 validation context")
        return validate_reply, ()
    is_intent = selection == INTENT_VALIDATOR
    if selection not in {BINDING_VALIDATOR, INTENT_VALIDATOR} or plan.get("mode") != (
        "preference_intent_development_v3" if is_intent else "preference_binding_development_v2"
    ):
        raise ValueError("unsupported validation policy")
    from benchmarks.internal import preference_semantic_binding_v2 as binding

    expected_stop = INTENT_EARLY_STOP if is_intent else {
        "first_case_ids": list(BINDING_EARLY_CASE_IDS), "stop_if_all_have_zero_assessments": True}
    if plan.get("early_stop") != expected_stop:
        raise ValueError("binding early-stop policy changed")
    if is_intent and (len(jobs) != 24 or len({job[0] for job in jobs}) != 24
                      or plan.get("case_count") != 24 or plan.get("request_count") != 24 or plan.get("cap_usd") != "0.50"
                      or plan.get("candidate_routes_are_write_permission") is not False):
        raise ValueError("intent development bounds changed")
    if is_intent and plan.get("limits") != {
        "attempts_per_eligible_case": 1, "sdk_retries": 0, "concurrency": 1,
        "request_timeout_seconds": 30, "fallback_models": False,
        "automatic_reruns": False, "stop_on_transport_or_output_failure": True,
    }:
        raise ValueError("intent execution limits changed")
    if tuple(job[0] for job in jobs[:3]) != BINDING_EARLY_CASE_IDS:
        raise ValueError("binding smoke case order changed")
    sources = plan.get("source_sha256_utf8_lf", {})
    for path in INTENT_VALIDATION_SOURCES if is_intent else BINDING_VALIDATION_SOURCES:
        if sources.get(path) != prep.digest((prep.ROOT / path).read_text(encoding="utf-8")):
            raise ValueError("binding validation source changed")
    for (_, shown, request), row in zip(jobs, plan["cases"], strict=True):
        if not isinstance(shown, binding.BindingSnapshot):
            raise ValueError("unexpected v2 validation context")  # noqa: TRY004 - invalid sealed plan
        snapshot = binding.BindingSnapshot.model_validate(shown.model_dump())
        if not binding.complete(snapshot) or request is None or not row.get("context_sha256") or not row.get("wire_sha256"):
            raise ValueError("binding validation snapshot or wire seal missing")
        if is_intent and any(request.get(key) != value for key, value in {
            "model": "gpt-4.1-mini-2025-04-14", "temperature": 0, "max_completion_tokens": 1024,
            "n": 1, "stream": False, "service_tier": "default",
        }.items()):
            raise ValueError("intent request candidate changed")
    if is_intent:
        from benchmarks.internal.preference_semantic_intent_v3 import (
            validate_reply as validate_intent,
        )

        return validate_intent, BINDING_EARLY_CASE_IDS
    return binding.validate_reply, BINDING_EARLY_CASE_IDS


def prepare_execution() -> tuple[dict, list]:
    base = prep.build_preflight()
    if base["preflight_sha256"] != APPROVED_BASE:
        raise ValueError("approved base preflight changed")
    pack, _ = load_development_pack()
    instructions = prep.INSTRUCTIONS.read_text(encoding="utf-8")
    jobs, rows = [], []
    for raw, row in zip(sorted(pack["cases"], key=lambda item: item["case_id"]), base["cases"], strict=True):
        case = DraftCase.model_validate(raw)
        shown = context_for(pack, case)
        request = prep.build_request(shown, instructions=instructions, candidate=base["candidate"])
        if row["case_id"] != case.case_id:
            raise ValueError("case order changed")
        if request is None:
            if row["action"] != "no_call":
                raise ValueError("no-call coverage changed")
            rows.append(row)
            jobs.append((case.case_id, shown, None))
            continue
        if prep.digest(prep.canonical(request)) != row["request_sha256"]:
            raise ValueError("base request changed")
        # Explicit default is necessary to bound token charges at standard rates.
        # It changes request hashes; the execution digest needs its own approval.
        request = {**request, "service_tier": "default"}
        rows.append({"case_id": case.case_id, "action": "candidate_request",
                     "request_sha256": prep.digest(prep.canonical(request))})
        jobs.append((case.case_id, shown, request))
    plan = {
        "mode": "one_shot_exploratory_development_only",
        "base_preflight_sha256": APPROVED_BASE,
        "executor_sha256_utf8_lf": prep.digest(Path(__file__).read_text(encoding="utf-8")),
        "candidate": base["candidate"], "request_amendment": {"service_tier": "default"},
        "cap_usd": str(CAP), "per_attempt_reservation_usd": str(RESERVATION),
        "reservation_prompt_token_ceiling": MAX_PROMPT_TOKENS,
        "limits": base["future_execution_limits"],
        "cost_basis": "published standard text rates, uncached upper bound; not an invoice",
        "first_request_is_schema_smoke": True,
        "stop_on_any_frozen_validator_rejection": True,
        "human_release_gate_met": False, "semantic_accuracy_measured": False,
        "holdouts_loaded": False, "ai_annotations_loaded": False,
        "cases": rows,
    }
    return {**plan, "execution_sha256": prep.digest(prep.canonical(plan))}, jobs


def _unique_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def parse_json(text: str):
    return json.loads(text, object_pairs_hook=_unique_pairs,
                      parse_constant=lambda value: (_ for _ in ()).throw(ValueError("non-finite JSON")))


def usage_cost(payload: dict) -> tuple[dict, Decimal]:
    if payload.get("service_tier") != "default":
        raise ValueError("unverified_service_tier")
    usage = payload.get("usage")
    if not isinstance(usage, dict):
        raise ValueError("missing_usage")  # noqa: TRY004 - malformed wire data
    prompt, completion, total = (usage.get(key) for key in ("prompt_tokens", "completion_tokens", "total_tokens"))
    if any(type(value) is not int or value < 0 for value in (prompt, completion, total)):
        raise ValueError("invalid_usage")
    if prompt > MAX_PROMPT_TOKENS or completion > MAX_OUTPUT_TOKENS or total != prompt + completion:
        raise ValueError("usage_outside_reserved_bounds")
    # Ignore cache discounts conservatively; report upper bound, never exact bill.
    return {"prompt_tokens": prompt, "completion_tokens": completion, "total_tokens": total}, (
        prompt * INPUT_RATE + completion * OUTPUT_RATE) / 1_000_000


def decode_reply(payload: dict) -> dict:
    choices = payload.get("choices")
    if not isinstance(choices, list) or len(choices) != 1 or not isinstance(choices[0], dict):
        raise ValueError("invalid_choices")
    choice = choices[0]
    message = choice.get("message")
    if choice.get("finish_reason") != "stop":
        raise ValueError("incomplete_or_filtered_output")
    if not isinstance(message, dict) or message.get("role") != "assistant":
        raise ValueError("invalid_message")
    if message.get("refusal") or message.get("tool_calls") or message.get("function_call"):
        raise ValueError("refusal_or_unexpected_tool_call")
    content = message.get("content")
    if not isinstance(content, str):
        raise ValueError("missing_content")  # noqa: TRY004 - malformed wire data
    reply = parse_json(content)
    if not isinstance(reply, dict):
        raise ValueError("invalid_reply_shape")  # noqa: TRY004 - malformed wire data
    return reply


def fetch(client: httpx.Client, request: dict, api_key: str, *, wire_body: bytes | None = None) -> dict:
    body_to_send = prep.canonical(request).encode("utf-8") if wire_body is None else wire_body
    if wire_body is not None and prep.canonical(parse_json(wire_body.decode("utf-8"))) != prep.canonical(request):
        raise ValueError("wire body differs from request")
    with client.stream("POST", "https://api.openai.com/v1/chat/completions",
                       headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                       content=body_to_send, timeout=30) as response:
        if response.status_code != 200:
            # No response headers, request headers or provider error text in logs.
            raise ValueError(f"http_status_{response.status_code}")
        body = bytearray()
        for chunk in response.iter_bytes():
            body.extend(chunk)
            if len(body) > MAX_RESPONSE_BYTES:
                raise ValueError("response_too_large")
        payload = parse_json(body.decode("utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("invalid_response_envelope")  # noqa: TRY004 - malformed wire data
        if payload.get("model") != "gpt-4.1-mini-2025-04-14":
            raise ValueError("unexpected_response_model")
        return payload


def durable_write(path: Path, value: dict) -> None:
    with path.open("x", encoding="utf-8", newline="\n") as handle:
        handle.write(prep.canonical(value) + "\n")
        handle.flush()
        os.fsync(handle.fileno())


def execute(plan: dict, jobs: list, *, approved_digest: str, client: httpx.Client,
            api_key: str, output_root: Path = OUTPUT_ROOT, cap: Decimal = CAP,
            wire_bodies: dict[str, bytes] | None = None) -> dict:
    if approved_digest != plan["execution_sha256"] or approved_digest != prep.digest(
        prep.canonical({key: value for key, value in plan.items() if key != "execution_sha256"})
    ):
        raise ValueError("execution approval digest mismatch")
    is_intent = plan.get("validator") == INTENT_VALIDATOR
    if plan.get("validator") in {BINDING_VALIDATOR, INTENT_VALIDATOR}:
        # Seal the complete trusted fixture view for this offline run, including
        # nested dictionaries inside otherwise frozen Pydantic wrappers.
        plan, jobs = deepcopy(plan), deepcopy(jobs)
    if not api_key.strip():
        raise ValueError("missing provider credential")
    # Check all bodies before creating the single-use lock or calling a provider.
    if not cap.is_finite() or not 0 < cap <= CAP:
        raise ValueError("unsupported spending cap")
    if len(jobs) != len(plan["cases"]) or plan["cap_usd"] != str(cap):
        raise ValueError("execution coverage or budget changed")
    validator, early_cases = validation_policy(plan, jobs)
    if wire_bodies is not None:
        wire_bodies = dict(wire_bodies)  # Snapshot before validation/dispatch.
        if set(wire_bodies) != {case_id for case_id, _, request in jobs if request is not None}:
            raise ValueError("wire body coverage changed")
    for (case_id, shown, request), row in zip(jobs, plan["cases"], strict=True):
        if case_id != row["case_id"] or (request is None) != (row["action"] == "no_call"):
            raise ValueError("execution coverage changed")
        if request is not None and (request.get("service_tier") != "default" or
                prep.digest(prep.canonical(request)) != row["request_sha256"]):
            raise ValueError("execution request changed")
        if "context_sha256" in row and prep.digest(prep.canonical(shown.model_dump(mode="json"))) != row["context_sha256"]:
            raise ValueError("offline validation context changed")
        if wire_bodies is None:
            if "wire_sha256" in row:
                raise ValueError("frozen wire body missing")
        elif request is not None:
            body = wire_bodies[case_id]
            if type(body) is not bytes:
                raise ValueError("wire body must be immutable bytes")
            text = body.decode("utf-8")
            if prep.digest(text) != row.get("wire_sha256") or prep.canonical(parse_json(text)) != prep.canonical(request):
                raise ValueError("frozen wire body changed")
    output_root.mkdir(parents=True, exist_ok=True)
    run_dir = output_root / f"run-{approved_digest}"
    if run_dir.resolve().parent != output_root.resolve():
        raise ValueError("output escaped experiment directory")
    run_dir.mkdir()  # Atomic, permanent one-shot lock; no resume/overwrite path.
    durable_write(run_dir / "manifest.json", {**plan, "started_at": datetime.now(UTC).isoformat()})
    known_cost, pending = Decimal(0), Decimal(0)
    attempts = validated = assessed_responses = assessment_count = abstention_count = controls = 0
    route_counts, usable_candidates = {}, 0
    reason, status = None, "complete"
    journal_path = run_dir / "journal.jsonl"
    with journal_path.open("x", encoding="utf-8", newline="\n") as journal:
        def record(value):
            journal.write(prep.canonical(value) + "\n")
            journal.flush()
            os.fsync(journal.fileno())

        for case_id, shown, request in jobs:
            if request is None:
                record({"case_id": case_id, "event": "no_call"})
                controls += 1
                continue
            if known_cost + RESERVATION > cap:
                status, reason = "stopped", "budget_reservation_unavailable"
                break
            pending = RESERVATION
            record({"case_id": case_id, "event": "reserved_before_dispatch", "reserved_usd": str(pending)})
            attempts += 1
            started = time.monotonic()
            payload = None
            result = {"case_id": case_id, "event": "response"}
            try:
                payload = (fetch(client, request, api_key) if wire_bodies is None else
                           fetch(client, request, api_key, wire_body=wire_bodies[case_id]))
                usage, charge = usage_cost(payload)
                known_cost += charge
                pending = Decimal(0)
                result.update(usage=usage, uncached_token_cost_upper_bound_usd=str(charge))
                reply = decode_reply(payload)
                decision = validator(reply, shown=shown, current=shown)
                result.update(reply=reply, decision=decision.model_dump(mode="json"))
                if decision.status != "attributed_proposals_only":
                    raise ValueError("frozen_validator_rejected")
                validated += 1
                findings = decision.proposals if is_intent else decision.assessments
                assessed_responses += bool(findings)
                assessment_count += len(findings)
                abstention_count += len(decision.abstentions)
                if is_intent:
                    for finding in findings:
                        route = finding.candidate_route
                        route_counts[route] = route_counts.get(route, 0) + 1
                        usable_candidates += route not in {"review_required", "no_current_change"}
                # Structural futility only, not a semantic pass/fail metric. The
                # three smoke requests are always first; controls cannot replace them.
                if early_cases and attempts == len(early_cases):
                    if is_intent and usable_candidates == 0:
                        status, reason = "stopped", "intent_smoke_no_usable_candidates"
                    elif not is_intent and assessed_responses == 0:
                        status, reason = "stopped", "binding_smoke_all_abstained"
            except (httpx.HTTPError, ValueError, UnicodeError) as exc:
                status = "stopped"
                # Exception text from transports can expose URLs/headers. Log type only.
                reason = str(exc) if isinstance(exc, ValueError) and str(exc) in {
                    "unverified_service_tier", "missing_usage", "invalid_usage", "usage_outside_reserved_bounds",
                    "invalid_choices", "incomplete_or_filtered_output", "invalid_message", "missing_content",
                    "refusal_or_unexpected_tool_call", "invalid_reply_shape", "frozen_validator_rejected",
                    "response_too_large", "invalid_response_envelope", "unexpected_response_model",
                    "duplicate JSON key", "non-finite JSON",
                } else type(exc).__name__
                if isinstance(exc, ValueError) and str(exc).startswith("http_status_"):
                    reason = str(exc)
                result["failure"] = reason
            result.update(latency_ms=round((time.monotonic() - started) * 1000, 2),
                          uncertain_charge_reservation_usd=str(pending))
            if payload is not None:
                result["provider_response"] = payload  # Synthetic outputs only, no HTTP headers.
            record(result)
            if status == "stopped":
                break
    summary = {
        "execution_sha256": approved_digest, "status": status, "stop_reason": reason,
        "provider_attempts": attempts, "validator_passed_responses": validated,
        "responses_with_assessments": assessed_responses,
        "assessment_findings": assessment_count, "abstention_findings": abstention_count,
        "no_call_controls_seen": controls, "total_cases": len(jobs),
        "uncached_token_cost_upper_bound_usd": str(known_cost),
        "uncertain_charge_reservation_usd": str(pending), "cap_usd": str(cap),
        "journal_sha256": prep.digest(journal_path.read_text(encoding="utf-8")),
        "semantic_accuracy_measured": False, "human_release_gate_met": False,
        "production_enablement_allowed": False, "holdouts_loaded": False, "ai_annotations_loaded": False,
    }
    if is_intent:
        summary.update(intent_proposal_findings=assessment_count, candidate_route_counts=route_counts,
                       usable_candidate_findings=usable_candidates, candidate_routes_are_write_permission=False)
    durable_write(run_dir / "summary.json", summary)
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--approved-execution", help="Exact execution digest explicitly approved by the user")
    args = parser.parse_args()
    try:
        plan, jobs = prepare_execution()
        if not args.execute:
            print(json.dumps({key: value for key, value in plan.items() if key != "cases"}, indent=2))
            return
        if args.approved_execution != plan["execution_sha256"]:
            raise ValueError("explicit matching execution approval required")
        from api.settings import get_settings

        # No project runtime LLM client, retries, proxy overrides or redirect following.
        with httpx.Client(transport=httpx.HTTPTransport(retries=0), trust_env=False,
                          follow_redirects=False, timeout=30) as client:
            summary = execute(plan, jobs, approved_digest=args.approved_execution,
                              client=client, api_key=get_settings().openai_api_key)
        print(json.dumps(summary, indent=2))
        if summary["status"] != "complete":
            parser.exit(1, "Exploratory run stopped; do not rerun this authorization.\n")
    except (OSError, ValueError) as exc:
        parser.exit(1, f"Exploratory execution blocked: {type(exc).__name__}\n")


if __name__ == "__main__":
    main()
