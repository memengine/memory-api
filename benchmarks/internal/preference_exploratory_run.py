"""One-shot synthetic development executor; no retries, labels or memory writes."""
from __future__ import annotations

import argparse
import json
import os
import time
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


def fetch(client: httpx.Client, request: dict, api_key: str) -> dict:
    with client.stream("POST", "https://api.openai.com/v1/chat/completions",
                       headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                       content=prep.canonical(request).encode("utf-8"), timeout=30) as response:
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
            api_key: str, output_root: Path = OUTPUT_ROOT) -> dict:
    if approved_digest != plan["execution_sha256"] or approved_digest != prep.digest(
        prep.canonical({key: value for key, value in plan.items() if key != "execution_sha256"})
    ):
        raise ValueError("execution approval digest mismatch")
    if not api_key.strip():
        raise ValueError("missing provider credential")
    # Check all bodies before creating the single-use lock or calling a provider.
    if len(jobs) != len(plan["cases"]) or plan["cap_usd"] != str(CAP):
        raise ValueError("execution coverage or budget changed")
    for (case_id, _, request), row in zip(jobs, plan["cases"], strict=True):
        if case_id != row["case_id"] or (request is None) != (row["action"] == "no_call"):
            raise ValueError("execution coverage changed")
        if request is not None and (request.get("service_tier") != "default" or
                prep.digest(prep.canonical(request)) != row["request_sha256"]):
            raise ValueError("execution request changed")
    output_root.mkdir(parents=True, exist_ok=True)
    run_dir = output_root / f"run-{approved_digest}"
    if run_dir.resolve().parent != output_root.resolve():
        raise ValueError("output escaped experiment directory")
    run_dir.mkdir()  # Atomic, permanent one-shot lock; no resume/overwrite path.
    durable_write(run_dir / "manifest.json", {**plan, "started_at": datetime.now(UTC).isoformat()})
    known_cost, pending = Decimal(0), Decimal(0)
    attempts = accepted = controls = 0
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
            if known_cost + RESERVATION > CAP:
                status, reason = "stopped", "budget_reservation_unavailable"
                break
            pending = RESERVATION
            record({"case_id": case_id, "event": "reserved_before_dispatch", "reserved_usd": str(pending)})
            attempts += 1
            started = time.monotonic()
            payload = None
            result = {"case_id": case_id, "event": "response"}
            try:
                payload = fetch(client, request, api_key)
                usage, charge = usage_cost(payload)
                known_cost += charge
                pending = Decimal(0)
                result.update(usage=usage, uncached_token_cost_upper_bound_usd=str(charge))
                reply = decode_reply(payload)
                decision = validate_reply(reply, shown=shown, current=shown)
                result.update(reply=reply, decision=decision.model_dump(mode="json"))
                if decision.status != "attributed_proposals_only":
                    raise ValueError("frozen_validator_rejected")
                accepted += 1
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
        "provider_attempts": attempts, "accepted_offline_proposals": accepted,
        "no_call_controls_seen": controls, "total_cases": len(jobs),
        "uncached_token_cost_upper_bound_usd": str(known_cost),
        "uncertain_charge_reservation_usd": str(pending), "cap_usd": str(CAP),
        "journal_sha256": prep.digest(journal_path.read_text(encoding="utf-8")),
        "semantic_accuracy_measured": False, "human_release_gate_met": False,
        "production_enablement_allowed": False, "holdouts_loaded": False, "ai_annotations_loaded": False,
    }
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
