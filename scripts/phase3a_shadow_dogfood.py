"""Exercise Phase 3A proposal confirmation through the real tenant API.

This is a plumbing and observability check, not a release benchmark. It sends
synthetic proposal/response conversations, waits for their extraction jobs,
and writes only bounded shadow observations. Raw fixture text is never copied
to the result artifact.

Environment:
  MEMORYOS_API_BASE_URL  API origin (default: https://api.memoryo.dev)
  MEMORYOS_API_KEY       Required only with --execute
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

DEFAULT_FIXTURE = (
    Path(__file__).with_name("fixtures") / "phase3a_shadow_dogfood_v1.json"
)
TERMINAL_STATUSES = {"completed", "failed", "error", "blocked", "dead_letter"}
EXPECTED_LANGUAGES = {"en", "hi", "hinglish"}
EXPECTED_REFERENCE_TYPES = {
    "single_vague",
    "single_indirect",
    "explicit_ordinal",
    "ambiguous_multi",
    "rejection",
}
OBSERVATION_FIELDS = {
    "enabled",
    "eligible",
    "attempted",
    "write_blocked",
    "active_proposal_count",
    "status",
    "outcome",
    "accepted_candidate_count",
    "pending_candidate_count",
    "rejected_candidate_count",
    "rejected_reasons",
    "model_marked_nothing_to_extract",
    "tokens_used",
    "provider_used",
    "latency_ms",
    "gate_reason",
    "error_type",
}


def load_fixture(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    cases = data.get("cases")
    if not isinstance(cases, list) or not cases:
        raise ValueError("Fixture must contain a non-empty cases list")

    seen_ids: set[str] = set()
    coverage: Counter[tuple[str, str]] = Counter()
    for case in cases:
        case_id = str(case.get("id") or "").strip()
        if not case_id or case_id in seen_ids:
            raise ValueError(f"Case IDs must be present and unique: {case_id!r}")
        seen_ids.add(case_id)

        language = str(case.get("language") or "")
        reference_type = str(case.get("reference_type") or "")
        if language not in EXPECTED_LANGUAGES:
            raise ValueError(f"Unsupported language in {case_id}: {language}")
        if reference_type not in EXPECTED_REFERENCE_TYPES:
            raise ValueError(
                f"Unsupported reference type in {case_id}: {reference_type}"
            )
        coverage[(language, reference_type)] += 1

        expected_outcome = str(case.get("expected_outcome") or "")
        if expected_outcome not in {"accepted", "pending", "rejected"}:
            raise ValueError(f"Unsupported expected outcome in {case_id}")
        if not str(case.get("user_utterance") or "").strip():
            raise ValueError(f"Missing user utterance in {case_id}")

        proposals = case.get("proposals")
        expected_count = 2 if reference_type in {"explicit_ordinal", "ambiguous_multi"} else 1
        if not isinstance(proposals, list) or len(proposals) != expected_count:
            raise ValueError(
                f"{case_id} requires exactly {expected_count} proposal(s)"
            )
        for proposal in proposals:
            visible = str(proposal.get("assistant_text") or "").strip()
            memory = str(proposal.get("memory") or "").strip()
            if len(memory) < 10 or memory.casefold() not in visible.casefold():
                raise ValueError(
                    f"{case_id} proposal memory must appear in assistant_text"
                )
            if proposal.get("category") not in {
                "preference",
                "fact",
                "goal",
                "procedure",
                "relationship",
                "expertise",
            }:
                raise ValueError(f"Unsupported proposal category in {case_id}")

    required = {
        (language, reference_type)
        for language in EXPECTED_LANGUAGES
        for reference_type in EXPECTED_REFERENCE_TYPES
    }
    if set(coverage) != required or any(count != 1 for count in coverage.values()):
        raise ValueError("Fixture must contain one case for each language/reference slice")
    return data


def build_payload(case: dict[str, Any], run_id: str) -> dict[str, Any]:
    case_id = str(case["id"])
    messages: list[dict[str, Any]] = []
    for ordinal, proposal in enumerate(case["proposals"], 1):
        messages.append(
            {
                "role": "assistant",
                "content": proposal["assistant_text"],
                "external_turn_id": f"{run_id}:{case_id}:proposal:{ordinal}",
                "source_kind": "assistant_output",
                "is_memory_proposal": True,
                "proposed_memory": {
                    "content": proposal["memory"],
                    "category": proposal["category"],
                },
            }
        )
    messages.append(
        {
            "role": "user",
            "content": case["user_utterance"],
            "external_turn_id": f"{run_id}:{case_id}:user",
            "source_kind": "direct_user_input",
        }
    )
    return {
        "external_user_id": f"phase3a-shadow-{run_id}-{case_id}",
        "conversation_id": f"phase3a-shadow:{run_id}:{case_id}",
        "messages": messages,
        "metadata": {
            "traffic_class": "phase3a_shadow_dogfood",
            "fixture_version": "v1",
            "case_id": case_id,
            "language": case["language"],
            "reference_type": case["reference_type"],
        },
        "evidence_mode": "conversation_evidence",
    }


def bounded_observation(job: dict[str, Any]) -> dict[str, Any]:
    metadata = job.get("extraction_metadata")
    if not isinstance(metadata, dict):
        return {}
    observation = metadata.get("phase3a_confirmation_shadow")
    if not isinstance(observation, dict):
        return {}
    return {
        key: value
        for key, value in observation.items()
        if key in OBSERVATION_FIELDS
    }


def write_artifact(
    output: Path,
    *,
    fixture_version: str,
    run_id: str,
    base_url: str,
    results: list[dict[str, Any]],
) -> None:
    outcome_counts: Counter[str] = Counter()
    for result in results:
        shadow = result.get("shadow") or {}
        outcome = str(
            shadow.get("outcome")
            or shadow.get("status")
            or result.get("request_status")
            or "missing"
        )
        outcome_counts[outcome] += 1
    artifact = {
        "schema_version": 1,
        "purpose": "phase3a_shadow_production_plumbing",
        "release_evidence": False,
        "fixture_version": fixture_version,
        "run_id": run_id,
        "base_url": base_url.rstrip("/"),
        "updated_at": datetime.now(UTC).isoformat(),
        "case_count": len(results),
        "outcome_counts": dict(sorted(outcome_counts.items())),
        "results": results,
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(artifact, indent=2, sort_keys=True), encoding="utf-8")


async def wait_for_job(
    client: httpx.AsyncClient,
    job_id: str,
    *,
    poll_seconds: float,
    timeout_seconds: float,
) -> dict[str, Any]:
    deadline = asyncio.get_running_loop().time() + timeout_seconds
    while True:
        response = await client.get(f"/v1/memories/jobs/{job_id}")
        response.raise_for_status()
        body = response.json()
        job = body.get("data", body)
        if str(job.get("status", "")).lower() in TERMINAL_STATUSES:
            return job
        if asyncio.get_running_loop().time() >= deadline:
            return {"job_id": job_id, "status": "poll_timeout"}
        await asyncio.sleep(poll_seconds)


async def run(args: argparse.Namespace) -> int:
    fixture = load_fixture(Path(args.fixture))
    cases = fixture["cases"]
    if args.case_id:
        requested = set(args.case_id)
        known = {str(case["id"]) for case in cases}
        unknown = sorted(requested - known)
        if unknown:
            raise SystemExit(f"Unknown case ID(s): {', '.join(unknown)}")
        cases = [case for case in cases if case["id"] in requested]
    if not args.execute:
        print(
            f"Validated {len(cases)} cases. No requests sent; pass --execute to run."
        )
        return 0

    api_key = os.environ.get("MEMORYOS_API_KEY", "").strip()
    if not api_key:
        raise SystemExit("MEMORYOS_API_KEY is required with --execute")

    run_id = args.run_id or datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    headers = {"Authorization": f"ApiKey {api_key}"}
    results: list[dict[str, Any]] = []
    output = Path(args.output) if args.output else Path(
        f"artifacts/internal-benchmarks/phase3a/phase3a-shadow-dogfood-{run_id}.json"
    )

    async with httpx.AsyncClient(
        base_url=args.base_url.rstrip("/"),
        headers=headers,
        timeout=args.request_timeout,
    ) as client:
        # Authenticate without creating a user or extraction job.
        preflight = await client.get(
            "/v1/memories",
            params={"external_user_id": f"phase3a-shadow-preflight-{run_id}", "limit": 1},
        )
        preflight.raise_for_status()

        for index, case in enumerate(cases, 1):
            payload = build_payload(case, run_id)
            response = await client.post(
                "/v1/memories/add",
                json=payload,
                headers={"Idempotency-Key": f"phase3a-shadow-v1:{run_id}:{case['id']}"},
            )
            response.raise_for_status()
            body = response.json()
            queued = body.get("data", body)
            job_id = str(queued.get("job_id") or "")
            if not job_id:
                request_status = str(queued.get("status") or "blocked")
                results.append(
                    {
                        "case_id": case["id"],
                        "language": case["language"],
                        "reference_type": case["reference_type"],
                        "expected_outcome": case["expected_outcome"],
                        "job_id": None,
                        "job_status": None,
                        "request_status": request_status,
                        "blocked_reason": str(queued.get("blocked_reason") or "unknown")[:128],
                        "retry_after_seconds": queued.get("retry_after_seconds"),
                        "memories_created": 0,
                        "shadow": {},
                    }
                )
                write_artifact(
                    output,
                    fixture_version=str(fixture.get("version", "v1")),
                    run_id=run_id,
                    base_url=args.base_url,
                    results=results,
                )
                print(
                    f"[{index:02d}/{len(cases)}] {case['id']} "
                    f"request={request_status} shadow=not_run"
                )
                if args.fail_fast:
                    break
                continue
            job = await wait_for_job(
                client,
                job_id,
                poll_seconds=args.poll_seconds,
                timeout_seconds=args.job_timeout,
            )
            observation = bounded_observation(job)
            outcome = str(observation.get("outcome") or observation.get("status") or "missing")
            results.append(
                {
                    "case_id": case["id"],
                    "language": case["language"],
                    "reference_type": case["reference_type"],
                    "expected_outcome": case["expected_outcome"],
                    "job_id": job_id,
                    "job_status": str(job.get("status") or "unknown"),
                    "memories_created": int(job.get("memories_created", 0) or 0),
                    "shadow": observation,
                }
            )
            write_artifact(
                output,
                fixture_version=str(fixture.get("version", "v1")),
                run_id=run_id,
                base_url=args.base_url,
                results=results,
            )
            print(
                f"[{index:02d}/{len(cases)}] {case['id']} "
                f"job={job.get('status', 'unknown')} shadow={outcome}"
            )
            if args.delay_seconds:
                await asyncio.sleep(args.delay_seconds)

    write_artifact(
        output,
        fixture_version=str(fixture.get("version", "v1")),
        run_id=run_id,
        base_url=args.base_url,
        results=results,
    )
    print(f"Wrote bounded shadow artifact: {output}")

    failed_jobs = sum(
        result.get("job_status") not in {"completed"}
        for result in results
    )
    missing_observations = sum(not result["shadow"] for result in results)
    unexpected_writes = sum(
        int(result.get("memories_created", 0) or 0) > 0
        for result in results
    )
    return 1 if failed_jobs or missing_observations or unexpected_writes else 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--base-url",
        default=os.environ.get("MEMORYOS_API_BASE_URL", "https://api.memoryo.dev"),
    )
    parser.add_argument("--fixture", default=str(DEFAULT_FIXTURE))
    parser.add_argument("--output")
    parser.add_argument("--run-id")
    parser.add_argument(
        "--case-id",
        action="append",
        help="Run only this fixture case; may be repeated.",
    )
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--poll-seconds", type=float, default=2.0)
    parser.add_argument("--job-timeout", type=float, default=180.0)
    parser.add_argument("--request-timeout", type=float, default=30.0)
    parser.add_argument("--delay-seconds", type=float, default=0.25)
    parser.add_argument("--fail-fast", action="store_true")
    return parser.parse_args()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(run(parse_args())))
