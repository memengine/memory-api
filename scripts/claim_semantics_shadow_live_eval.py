"""Evaluate the claim-semantics shadow contract through the public API.

This runner creates one fresh synthetic user per development case, submits one
normal memory-ingestion request, and scores only the diagnostic metadata
returned by the completed job. It never writes the API key to its artifact.

Environment:
  MEMORYOS_API_KEY       Required with --execute.
  MEMORYOS_API_BASE_URL  Optional API origin override.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import time
import uuid
from collections import Counter, defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DATASET = (
    ROOT
    / "benchmarks"
    / "internal"
    / "datasets"
    / "claim_semantics"
    / "development"
    / "development_v1.json"
)
DEFAULT_BASE_URL = "https://api.memoryo.dev"
TERMINAL_JOB_STATUSES = {"blocked", "completed", "dead", "dead_letter", "error"}
EXPECTED_FIELDS = ("predicate", "speech_act", "certainty", "temporal_kind")
TRANSIENT_STATUSES = {429, 500, 502, 503, 504}


def load_cases(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    cases = payload.get("cases")
    if not isinstance(cases, list) or not cases:
        raise ValueError("Dataset must contain at least one case")
    seen: set[str] = set()
    for case in cases:
        case_id = str(case.get("id") or "").strip()
        if not case_id or case_id in seen:
            raise ValueError("Each case must have a unique non-empty id")
        seen.add(case_id)
        if not str(case.get("language") or "").strip():
            raise ValueError(f"{case_id}.language must be non-empty")
        if not str(case.get("utterance") or "").strip():
            raise ValueError(f"{case_id}.utterance must be non-empty")
        expected = case.get("expected")
        if not isinstance(expected, dict) or any(
            not str(expected.get(field) or "").strip() for field in EXPECTED_FIELDS
        ):
            raise ValueError(f"{case_id}.expected must define {EXPECTED_FIELDS}")
    return payload


def select_cases(dataset: dict[str, Any], case_ids: list[str]) -> dict[str, Any]:
    if not case_ids:
        return dataset
    requested = set(case_ids)
    selected = [case for case in dataset["cases"] if case["id"] in requested]
    found = {case["id"] for case in selected}
    missing = sorted(requested - found)
    if missing:
        raise ValueError(f"Unknown case ids: {', '.join(missing)}")
    return {**dataset, "cases": selected}


def evaluate_shadow(
    metadata: dict[str, Any], expected: dict[str, str]
) -> dict[str, Any]:
    shadow = metadata.get("claim_semantics_shadow")
    if not isinstance(shadow, dict):
        return {
            "passed": False,
            "reason": "missing_shadow_metadata",
            "field_matches": {field: False for field in EXPECTED_FIELDS},
            "observation_count": 0,
        }
    observations = shadow.get("observations")
    accepted = observations if isinstance(observations, list) else []
    observation = (
        accepted[0] if len(accepted) == 1 and isinstance(accepted[0], dict) else {}
    )
    field_matches = {
        field: observation.get(field) == expected[field] for field in EXPECTED_FIELDS
    }
    exactly_one = len(accepted) == 1
    return {
        "passed": exactly_one and all(field_matches.values()),
        "reason": None if exactly_one else "expected_exactly_one_accepted_observation",
        "field_matches": field_matches,
        "observation_count": len(accepted),
        "observed": {field: observation.get(field) for field in EXPECTED_FIELDS},
    }


def _percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, round((len(ordered) - 1) * fraction)))
    return round(ordered[index], 2)


async def _request_with_retries(
    client: httpx.AsyncClient,
    method: str,
    url: str,
    *,
    attempts: int,
    **kwargs: Any,
) -> httpx.Response:
    for attempt in range(attempts):
        retry_after: str | None = None
        try:
            response = await client.request(method, url, **kwargs)
            if (
                response.status_code not in TRANSIENT_STATUSES
                or attempt == attempts - 1
            ):
                return response
            retry_after = response.headers.get("Retry-After")
        except (httpx.ConnectError, httpx.ReadTimeout):
            if attempt == attempts - 1:
                raise
        try:
            delay = float(retry_after) if retry_after is not None else 2.0**attempt
        except ValueError:
            delay = 2.0**attempt
        await asyncio.sleep(min(30.0, max(1.0, delay)))
    raise RuntimeError("request retry loop ended unexpectedly")


async def _wait_for_job(
    client: httpx.AsyncClient,
    job_id: str,
    *,
    poll_seconds: float,
    timeout_seconds: float,
    request_attempts: int,
) -> tuple[dict[str, Any], float]:
    started = time.perf_counter()
    deadline = asyncio.get_running_loop().time() + timeout_seconds
    while True:
        response = await _request_with_retries(
            client, "GET", f"/v1/memories/jobs/{job_id}", attempts=request_attempts
        )
        response.raise_for_status()
        body = response.json()
        job = body.get("data", body)
        status = str(job.get("status") or "unknown").lower()
        if status in TERMINAL_JOB_STATUSES:
            return job, round((time.perf_counter() - started) * 1000, 2)
        if asyncio.get_running_loop().time() >= deadline:
            return {**job, "status": "poll_timeout"}, round(
                (time.perf_counter() - started) * 1000, 2
            )
        await asyncio.sleep(poll_seconds)


async def _run_case(
    client: httpx.AsyncClient,
    case: dict[str, Any],
    *,
    run_id: str,
    poll_seconds: float,
    job_timeout: float,
    request_attempts: int,
) -> dict[str, Any]:
    case_id = case["id"]
    external_user_id = f"claim-shadow-dev-{run_id}-{case_id}"
    payload = {
        "external_user_id": external_user_id,
        "conversation_id": f"claim-shadow-dev:{run_id}:{case_id}",
        "messages": [{"role": "user", "content": case["utterance"]}],
        "evidence_mode": "conversation_evidence",
        "metadata": {
            "traffic_class": "synthetic_claim_semantics_development",
            "dataset_version": "claim-semantics-development-v1",
            "case_id": case_id,
        },
    }
    started = time.perf_counter()
    response = await _request_with_retries(
        client,
        "POST",
        "/v1/memories/add",
        attempts=request_attempts,
        json=payload,
        headers={"Idempotency-Key": f"claim-shadow-dev:{run_id}:{case_id}"},
    )
    acknowledgement_ms = round((time.perf_counter() - started) * 1000, 2)
    response.raise_for_status()
    acknowledgement = response.json()
    job_id = acknowledgement.get("job_id")
    if not job_id:
        raise RuntimeError(f"{case_id}: add response did not contain job_id")
    job, processing_ms = await _wait_for_job(
        client,
        str(job_id),
        poll_seconds=poll_seconds,
        timeout_seconds=job_timeout,
        request_attempts=request_attempts,
    )
    metadata = job.get("extraction_metadata")
    if not isinstance(metadata, dict):
        metadata = {}
    evaluation = evaluate_shadow(metadata, case["expected"])
    return {
        "case_id": case_id,
        "language": case["language"],
        "expected": case["expected"],
        "job_id": str(job_id),
        "job_status": job.get("status"),
        "job_attempts": job.get("attempts"),
        "job_error": job.get("error") or job.get("error_summary"),
        "acknowledgement_ms": acknowledgement_ms,
        "processing_ms": processing_ms,
        "evaluation": evaluation,
        "shadow": metadata.get("claim_semantics_shadow"),
        "primary_pass": metadata.get("primary_pass"),
        "candidate_validation": metadata.get("candidate_validation"),
    }


def summarize(records: list[dict[str, Any]]) -> dict[str, Any]:
    field_totals = Counter()
    languages: dict[str, list[bool]] = defaultdict(list)
    rejection_counts = Counter()
    acknowledgement_latencies: list[float] = []
    processing_latencies: list[float] = []
    provider_latencies: list[float] = []
    input_tokens = output_tokens = 0
    schema_enforced = 0
    for record in records:
        evaluation = record["evaluation"]
        languages[record["language"]].append(bool(evaluation["passed"]))
        field_totals.update(
            field for field, matched in evaluation["field_matches"].items() if matched
        )
        shadow = record.get("shadow") or {}
        rejection_counts.update(shadow.get("rejection_counts") or {})
        acknowledgement_latencies.append(record["acknowledgement_ms"])
        processing_latencies.append(record["processing_ms"])
        primary = record.get("primary_pass") or {}
        schema_enforced += int(bool(primary.get("schema_enforced")))
        if isinstance(primary.get("latency_ms"), (int, float)):
            provider_latencies.append(float(primary["latency_ms"]))
        input_tokens += int(primary.get("input_tokens") or 0)
        output_tokens += int(primary.get("output_tokens") or 0)
    total = len(records)
    passed = sum(1 for record in records if record["evaluation"]["passed"])
    return {
        "case_count": total,
        "passed": passed,
        "exact_match_rate": round(passed / total, 4) if total else 0.0,
        "field_accuracy": {
            field: round(field_totals[field] / total, 4) if total else 0.0
            for field in EXPECTED_FIELDS
        },
        "by_language": {
            language: {
                "passed": sum(results),
                "total": len(results),
                "exact_match_rate": round(sum(results) / len(results), 4),
            }
            for language, results in sorted(languages.items())
        },
        "rejection_counts": dict(sorted(rejection_counts.items())),
        "schema_enforcement": {"enforced": schema_enforced, "total": total},
        "latency_ms": {
            "acknowledgement_p50": _percentile(acknowledgement_latencies, 0.50),
            "acknowledgement_p95": _percentile(acknowledgement_latencies, 0.95),
            "processing_p50": _percentile(processing_latencies, 0.50),
            "processing_p95": _percentile(processing_latencies, 0.95),
            "provider_p50": _percentile(provider_latencies, 0.50),
            "provider_p95": _percentile(provider_latencies, 0.95),
        },
        "tokens": {
            "input": input_tokens,
            "output": output_tokens,
            "total": input_tokens + output_tokens,
        },
    }


async def execute(args: argparse.Namespace) -> dict[str, Any]:
    dataset = select_cases(load_cases(Path(args.dataset)), args.case_id)
    api_key = os.environ.get("MEMORYOS_API_KEY", "").strip()
    if not api_key:
        raise SystemExit("MEMORYOS_API_KEY is required with --execute")
    run_id = uuid.uuid4().hex[:12]
    started_at = datetime.now(UTC).isoformat()
    records: list[dict[str, Any]] = []
    headers = {"Authorization": f"ApiKey {api_key}"}
    async with httpx.AsyncClient(
        base_url=args.base_url.rstrip("/"),
        headers=headers,
        timeout=args.request_timeout,
    ) as client:
        for case in dataset["cases"]:
            try:
                record = await _run_case(
                    client,
                    case,
                    run_id=run_id,
                    poll_seconds=args.poll_seconds,
                    job_timeout=args.job_timeout,
                    request_attempts=args.request_attempts,
                )
            # A single malformed provider response must not erase the remaining
            # development-set evidence; the bounded error is recorded per case.
            except Exception as exc:  # noqa: BLE001
                record = {
                    "case_id": case["id"],
                    "language": case["language"],
                    "expected": case["expected"],
                    "evaluation": {
                        "passed": False,
                        "reason": f"runner_error:{type(exc).__name__}",
                        "field_matches": {field: False for field in EXPECTED_FIELDS},
                        "observation_count": 0,
                    },
                    "error": str(exc)[:500],
                    "acknowledgement_ms": 0.0,
                    "processing_ms": 0.0,
                }
            records.append(record)
            print(f"{record['case_id']}: passed={record['evaluation']['passed']}")
    return {
        "schema_version": 1,
        "dataset_schema_version": dataset.get("schema_version"),
        "split": dataset.get("split"),
        "run_id": run_id,
        "base_url": args.base_url.rstrip("/"),
        "started_at": started_at,
        "records": records,
        "summary": summarize(records),
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--dataset", default=str(DEFAULT_DATASET))
    parser.add_argument("--case-id", action="append", default=[])
    parser.add_argument(
        "--base-url", default=os.environ.get("MEMORYOS_API_BASE_URL", DEFAULT_BASE_URL)
    )
    parser.add_argument("--output")
    parser.add_argument("--poll-seconds", type=float, default=1.0)
    parser.add_argument("--job-timeout", type=float, default=120.0)
    parser.add_argument("--request-timeout", type=float, default=30.0)
    parser.add_argument("--request-attempts", type=int, default=5)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    dataset = select_cases(load_cases(Path(args.dataset)), args.case_id)
    if not args.execute:
        print(
            json.dumps(
                {
                    "mode": "dry_run",
                    "case_ids": [case["id"] for case in dataset["cases"]],
                },
                indent=2,
            )
        )
        return 0
    artifact = asyncio.run(execute(args))
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    output = (
        Path(args.output)
        if args.output
        else (
            ROOT
            / "artifacts"
            / "internal-benchmarks"
            / "claim-semantics"
            / f"development-{timestamp}.json"
        )
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    artifact["completed_at"] = datetime.now(UTC).isoformat()
    output.write_text(json.dumps(artifact, indent=2, sort_keys=True), encoding="utf-8")
    print(f"artifact={output}")
    print(json.dumps(artifact["summary"], sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
