"""Characterize governed-memory consistency through the public MemoryOS API.

This is a read/write diagnostic for fresh synthetic users. It does not modify
production behavior and never writes the API key to its artifact.

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
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_FIXTURE = Path(__file__).with_name("fixtures") / "governed_memory_phase0_v1.json"
DEFAULT_BASE_URL = "https://api.memoryo.dev"
TERMINAL_JOB_STATUSES = {"blocked", "completed", "dead", "dead_letter", "error", "failed"}


def load_fixture(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    scenarios = payload.get("scenarios")
    if not isinstance(scenarios, list) or not scenarios:
        raise ValueError("Fixture must contain at least one scenario")
    seen: set[str] = set()
    for scenario in scenarios:
        scenario_id = str(scenario.get("id") or "").strip()
        if not scenario_id or scenario_id in seen:
            raise ValueError("Each scenario must have a unique non-empty id")
        seen.add(scenario_id)
        for field in ("initial_messages", "update_messages"):
            messages = scenario.get(field)
            if not isinstance(messages, list) or not messages:
                raise ValueError(f"{scenario_id}.{field} must contain messages")
            if any(message.get("role") not in {"user", "assistant"} for message in messages):
                raise ValueError(f"{scenario_id}.{field} contains an unsupported role")
        for field in ("warm_query", "verification_query"):
            if not str(scenario.get(field) or "").strip():
                raise ValueError(f"{scenario_id}.{field} must be non-empty")
        expected = scenario.get("expected")
        if not isinstance(expected, dict) or not isinstance(
            expected.get("clarification_required"), bool
        ):
            raise TypeError(f"{scenario_id}.expected is invalid")
    return payload


def build_add_payload(
    scenario: dict[str, Any],
    *,
    external_user_id: str,
    conversation_id: str,
    phase: str,
) -> dict[str, Any]:
    messages_field = "initial_messages" if phase == "initial" else "update_messages"
    return {
        "external_user_id": external_user_id,
        "messages": scenario[messages_field],
        "conversation_id": conversation_id,
        "evidence_mode": "conversation_evidence",
        "metadata": {
            "traffic_class": "synthetic_phase0_characterization",
            "fixture_version": "phase0-v1",
            "scenario_id": scenario["id"],
            "phase": phase,
        },
    }


def build_retrieve_payload(
    *, external_user_id: str, query: str
) -> dict[str, Any]:
    return {
        "external_user_id": external_user_id,
        "query": query,
        "limit": 10,
        "context_max_tokens": 1000,
    }


def evaluate_retrieval(
    response: dict[str, Any], expected: dict[str, Any], *, check_clarification: bool = True
) -> dict[str, Any]:
    raw_memories = response.get("data")
    memories: list[dict[str, Any]] = (
        [item for item in raw_memories if isinstance(item, dict)]
        if isinstance(raw_memories, list)
        else []
    )
    searchable = "\n".join(str(item.get("content") or "") for item in memories).lower()
    include_terms = [str(item).lower() for item in expected.get("must_include_any", [])]
    exclude_terms = [str(item).lower() for item in expected.get("must_exclude_all", [])]
    clarification_present = bool(response.get("clarification"))
    checks: dict[str, bool] = {
        "required_value": not include_terms or any(term in searchable for term in include_terms),
        "superseded_value_absent": not any(term in searchable for term in exclude_terms),
    }
    if check_clarification:
        checks["clarification"] = clarification_present == bool(
            expected.get("clarification_required")
        )
    return {
        "passed": all(checks.values()),
        "checks": checks,
        "observed": {
            "clarification_present": clarification_present,
            "memory_count": len(memories),
            "matched_required_terms": [term for term in include_terms if term in searchable],
            "matched_excluded_terms": [term for term in exclude_terms if term in searchable],
        },
    }


def evaluate_scenario(
    immediate: dict[str, Any], settled: dict[str, Any], expected: dict[str, Any]
) -> dict[str, Any]:
    immediate_state = evaluate_retrieval(
        immediate, expected, check_clarification=False
    )
    settled_state = evaluate_retrieval(settled, expected, check_clarification=False)
    clarification_observations = [
        bool(immediate.get("clarification")),
        bool(settled.get("clarification")),
    ]
    clarification_required = bool(expected.get("clarification_required"))
    clarification_check = (
        any(clarification_observations)
        if clarification_required
        else not any(clarification_observations)
    )
    checks = {
        "immediate_memory_state": immediate_state["passed"],
        "settled_memory_state": settled_state["passed"],
        "clarification_delivered_at_least_once": clarification_check,
    }
    return {
        "passed": all(checks.values()),
        "checks": checks,
        "clarification_observations": clarification_observations,
        "immediate": immediate_state,
        "settled": settled_state,
    }


def _request_identity(response: httpx.Response, body: dict[str, Any]) -> dict[str, Any]:
    return {
        "request_id": body.get("request_id") or response.headers.get("x-request-id"),
        "http_status": response.status_code,
    }


async def wait_for_job(
    client: httpx.AsyncClient,
    job_id: str,
    *,
    poll_seconds: float,
    timeout_seconds: float,
) -> dict[str, Any]:
    started = time.perf_counter()
    deadline = asyncio.get_running_loop().time() + timeout_seconds
    while True:
        response = await client.get(f"/v1/memories/jobs/{job_id}")
        response.raise_for_status()
        body = response.json()
        job = body.get("data", body)
        status = str(job.get("status") or "unknown").lower()
        if status in TERMINAL_JOB_STATUSES:
            return {
                **_request_identity(response, body),
                "latency_ms": round((time.perf_counter() - started) * 1000, 2),
                "job": job,
            }
        if asyncio.get_running_loop().time() >= deadline:
            return {
                **_request_identity(response, body),
                "latency_ms": round((time.perf_counter() - started) * 1000, 2),
                "job": {**job, "status": "poll_timeout"},
            }
        await asyncio.sleep(poll_seconds)


async def add_and_wait(
    client: httpx.AsyncClient,
    payload: dict[str, Any],
    *,
    idempotency_key: str,
    poll_seconds: float,
    timeout_seconds: float,
) -> dict[str, Any]:
    started = time.perf_counter()
    response = await client.post(
        "/v1/memories/add",
        json=payload,
        headers={"Idempotency-Key": idempotency_key},
    )
    acknowledgement_ms = round((time.perf_counter() - started) * 1000, 2)
    response.raise_for_status()
    body = response.json()
    job_id = body.get("job_id")
    result: dict[str, Any] = {
        **_request_identity(response, body),
        "acknowledgement_ms": acknowledgement_ms,
        "job_id": job_id,
        "queue_status": body.get("status"),
        "processing_eta_seconds": body.get("processing_eta_seconds"),
        "processing_status": body.get("processing_status"),
    }
    if job_id and str(body.get("status") or "").lower() == "queued":
        result["terminal"] = await wait_for_job(
            client,
            str(job_id),
            poll_seconds=poll_seconds,
            timeout_seconds=timeout_seconds,
        )
    return result


async def retrieve(
    client: httpx.AsyncClient, *, external_user_id: str, query: str
) -> dict[str, Any]:
    started = time.perf_counter()
    response = await client.post(
        "/v1/memories/retrieve",
        json=build_retrieve_payload(external_user_id=external_user_id, query=query),
    )
    latency_ms = round((time.perf_counter() - started) * 1000, 2)
    response.raise_for_status()
    body = response.json()
    return {
        **_request_identity(response, body),
        "latency_ms": latency_ms,
        "retrieval_id": body.get("retrieval_id"),
        "cached": body.get("cached"),
        "is_degraded": body.get("is_degraded"),
        "is_passthrough": body.get("is_passthrough"),
        "clarification": body.get("clarification"),
        "data": body.get("data", []),
    }


async def execute(args: argparse.Namespace) -> dict[str, Any]:
    fixture = load_fixture(Path(args.fixture))
    api_key = os.environ.get("MEMORYOS_API_KEY", "").strip()
    if not api_key:
        raise SystemExit("MEMORYOS_API_KEY is required with --execute")

    run_id = uuid.uuid4().hex[:12]
    artifact: dict[str, Any] = {
        "schema_version": 1,
        "fixture_version": fixture["version"],
        "run_id": run_id,
        "started_at": datetime.now(UTC).isoformat(),
        "base_url": args.base_url.rstrip("/"),
        "scenarios": [],
    }
    headers = {"Authorization": f"ApiKey {api_key}"}
    async with httpx.AsyncClient(
        base_url=args.base_url.rstrip("/"), headers=headers, timeout=args.request_timeout
    ) as client:
        for scenario in fixture["scenarios"]:
            scenario_id = scenario["id"]
            external_user_id = f"phase0-{scenario_id}-{run_id}"
            conversation_id = f"phase0:{scenario_id}:{run_id}"
            initial = await add_and_wait(
                client,
                build_add_payload(
                    scenario,
                    external_user_id=external_user_id,
                    conversation_id=conversation_id,
                    phase="initial",
                ),
                idempotency_key=f"phase0:{run_id}:{scenario_id}:initial",
                poll_seconds=args.poll_seconds,
                timeout_seconds=args.job_timeout,
            )
            if args.index_settle_seconds:
                await asyncio.sleep(args.index_settle_seconds)
            warm = await retrieve(
                client,
                external_user_id=external_user_id,
                query=scenario["warm_query"],
            )
            update = await add_and_wait(
                client,
                build_add_payload(
                    scenario,
                    external_user_id=external_user_id,
                    conversation_id=conversation_id,
                    phase="update",
                ),
                idempotency_key=f"phase0:{run_id}:{scenario_id}:update",
                poll_seconds=args.poll_seconds,
                timeout_seconds=args.job_timeout,
            )
            immediate = await retrieve(
                client,
                external_user_id=external_user_id,
                query=scenario["verification_query"],
            )
            if args.index_settle_seconds:
                await asyncio.sleep(args.index_settle_seconds)
            settled = await retrieve(
                client,
                external_user_id=external_user_id,
                query=scenario["verification_query"],
            )
            record = {
                "scenario_id": scenario_id,
                "external_user_id": external_user_id,
                "conversation_id": conversation_id,
                "initial_add": initial,
                "warm_retrieval": warm,
                "update_add": update,
                "immediate_retrieval": immediate,
                "settled_retrieval": settled,
                "immediate_evaluation": evaluate_retrieval(immediate, scenario["expected"]),
                "settled_evaluation": evaluate_retrieval(settled, scenario["expected"]),
                "scenario_evaluation": evaluate_scenario(
                    immediate, settled, scenario["expected"]
                ),
            }
            artifact["scenarios"].append(record)
            print(
                f"{scenario_id}: immediate={record['immediate_evaluation']['passed']} "
                f"settled={record['settled_evaluation']['passed']}"
            )

    artifact["completed_at"] = datetime.now(UTC).isoformat()
    artifact["summary"] = {
        "scenario_count": len(artifact["scenarios"]),
        "immediate_passed": sum(
            1 for item in artifact["scenarios"] if item["immediate_evaluation"]["passed"]
        ),
        "settled_passed": sum(
            1 for item in artifact["scenarios"] if item["settled_evaluation"]["passed"]
        ),
        "scenarios_passed": sum(
            1 for item in artifact["scenarios"] if item["scenario_evaluation"]["passed"]
        ),
    }
    return artifact


def write_artifact(payload: dict[str, Any], output: Path) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument(
        "--base-url",
        default=os.environ.get("MEMORYOS_API_BASE_URL", DEFAULT_BASE_URL),
    )
    parser.add_argument("--fixture", default=str(DEFAULT_FIXTURE))
    parser.add_argument("--output")
    parser.add_argument("--poll-seconds", type=float, default=1.0)
    parser.add_argument("--job-timeout", type=float, default=120.0)
    parser.add_argument("--index-settle-seconds", type=float, default=6.0)
    parser.add_argument("--request-timeout", type=float, default=30.0)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    fixture = load_fixture(Path(args.fixture))
    if not args.execute:
        print(
            json.dumps(
                {
                    "mode": "dry_run",
                    "base_url": args.base_url.rstrip("/"),
                    "fixture_version": fixture["version"],
                    "scenario_ids": [item["id"] for item in fixture["scenarios"]],
                    "note": "Pass --execute with MEMORYOS_API_KEY set to create fresh synthetic users.",
                },
                indent=2,
            )
        )
        return 0

    artifact = asyncio.run(execute(args))
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    output = Path(args.output) if args.output else (
        ROOT / "artifacts" / "internal-benchmarks" / "phase0" / f"governed-memory-{timestamp}.json"
    )
    write_artifact(artifact, output)
    print(f"artifact={output}")
    print(json.dumps(artifact["summary"], sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
