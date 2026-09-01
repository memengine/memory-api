"""Replay deterministic, synthetic pre-launch conversations through MemoryOS.

Environment:
  MEMORYOS_API_BASE_URL  API origin (default: http://localhost:8000)
  MEMORYOS_API_KEY       Required API key
  MEMORYOS_TRAFFIC_FILE  Optional fixture override
"""

from __future__ import annotations

import argparse
import asyncio
from collections import Counter
from datetime import datetime
import json
import os
from pathlib import Path
from typing import Any

import httpx


DEFAULT_FIXTURE = Path(__file__).with_name("fixtures") / "prelaunch_traffic_v1.json"
TERMINAL_STATUSES = {"completed", "failed", "error", "blocked", "dead_letter"}


def load_fixture(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data.get("workflows"), list) or not data["workflows"]:
        raise ValueError("Fixture must contain a non-empty workflows list")
    seen: set[tuple[str, str]] = set()
    for workflow in data["workflows"]:
        for event in workflow.get("events", []):
            source = event["source"]
            key = (source["service"], source["event_id"])
            if key in seen and not event.get("duplicate_of"):
                raise ValueError(f"Duplicate source identity without duplicate_of: {key}")
            seen.add(key)
            datetime.fromisoformat(source["observed_at"].replace("Z", "+00:00"))
    return data


def build_payload(workflow: dict[str, Any], event: dict[str, Any]) -> dict[str, Any]:
    return {
        "external_user_id": workflow["user_id"],
        "messages": event["messages"],
        "metadata": {
            "traffic_class": "synthetic_prelaunch",
            "fixture_version": "v1",
            "workflow_id": workflow["id"],
            "category": workflow["category"],
            **event.get("metadata", {}),
        },
        "source": event["source"],
        **({"agent_id": event["agent_id"]} if event.get("agent_id") else {}),
    }


async def wait_for_job(
    client: httpx.AsyncClient, job_id: str, poll_seconds: float, timeout_seconds: float
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
            return {**job, "status": "poll_timeout"}
        await asyncio.sleep(poll_seconds)


async def replay(args: argparse.Namespace) -> int:
    fixture = load_fixture(Path(args.fixture))
    api_key = os.environ.get("MEMORYOS_API_KEY", "").strip()
    if not api_key:
        raise SystemExit("MEMORYOS_API_KEY is required")
    headers = {"Authorization": f"ApiKey {api_key}"}
    totals: Counter[str] = Counter()
    by_service: Counter[str] = Counter()
    by_category: Counter[str] = Counter()

    async with httpx.AsyncClient(
        base_url=args.base_url.rstrip("/"), headers=headers, timeout=args.request_timeout
    ) as client:
        for workflow_index, workflow in enumerate(fixture["workflows"], 1):
            workflow_ok = True
            for event_index, event in enumerate(workflow["events"], 1):
                totals["requests"] += 1
                service = event["source"]["service"]
                try:
                    response = await client.post(
                        "/v1/memories/add",
                        json=build_payload(workflow, event),
                        headers={"Idempotency-Key": f"prelaunch-v1:{service}:{event['source']['event_id']}"},
                    )
                    response.raise_for_status()
                    result = response.json()
                    status = str(result.get("status", "unknown")).lower()
                    job_id = result.get("job_id")
                    if args.wait and job_id and status == "queued":
                        job = await wait_for_job(client, job_id, args.poll_seconds, args.job_timeout)
                        status = str(job.get("status", status)).lower()
                        totals["memories_created"] += int(job.get("memories_created", 0) or 0)
                    if status in {"failed", "error", "dead_letter", "poll_timeout"}:
                        raise RuntimeError(f"job {job_id or '-'} ended as {status}")
                    totals["successful"] += 1
                    by_service[service] += 1
                    by_category[workflow["category"]] += 1
                except (httpx.HTTPError, RuntimeError, ValueError) as exc:
                    totals["failed"] += 1
                    workflow_ok = False
                    print(f"  FAIL {workflow['id']} event {event_index}: {exc}")
                    if args.fail_fast:
                        return 1
                if args.delay_seconds:
                    await asyncio.sleep(args.delay_seconds)
            totals["workflows"] += 1
            state = "ok" if workflow_ok else "partial"
            print(
                f"[{workflow_index:02d}/{len(fixture['workflows'])}] "
                f"{workflow['id']} ({len(workflow['events'])} requests) {state}"
            )

    print("\nReplay totals")
    print(
        f"workflows={totals['workflows']} requests={totals['requests']} "
        f"successful={totals['successful']} failed={totals['failed']} "
        f"memories_created={totals['memories_created'] if args.wait else 'not_polled'}"
    )
    print("by_service=" + ", ".join(f"{k}:{v}" for k, v in sorted(by_service.items())))
    print("by_category=" + ", ".join(f"{k}:{v}" for k, v in sorted(by_category.items())))
    return 1 if totals["failed"] else 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--base-url",
        default=os.environ.get("MEMORYOS_API_BASE_URL", "http://localhost:8000"),
    )
    parser.add_argument(
        "--fixture", default=os.environ.get("MEMORYOS_TRAFFIC_FILE", str(DEFAULT_FIXTURE))
    )
    parser.add_argument("--wait", action="store_true", help="Poll each extraction job to completion")
    parser.add_argument("--delay-seconds", type=float, default=0.0)
    parser.add_argument("--poll-seconds", type=float, default=1.0)
    parser.add_argument("--job-timeout", type=float, default=120.0)
    parser.add_argument("--request-timeout", type=float, default=30.0)
    parser.add_argument("--fail-fast", action="store_true")
    return parser.parse_args()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(replay(parse_args())))
