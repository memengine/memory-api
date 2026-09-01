"""Safely retry a replay phase with fresh source IDs and retry-aware polling."""
from __future__ import annotations

import argparse
import asyncio
import copy
import importlib.util
import sys
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
SPEC = importlib.util.spec_from_file_location("prelaunch_replay_v2", HERE / "prelaunch_traffic_replay_v2.py")
if SPEC is None or SPEC.loader is None:
    raise RuntimeError("Unable to load prelaunch_traffic_replay_v2.py")
replay_v2 = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = replay_v2
SPEC.loader.exec_module(replay_v2)


def scoped_fixture(data: dict[str, Any], run_id: str) -> dict[str, Any]:
    result = copy.deepcopy(data)
    for workflow in result["workflows"]:
        if workflow["feature"] == "universal":
            continue
        for event in workflow["events"]:
            event["source"]["event_id"] = f"{event['source']['event_id']}-{run_id}"
    return result


async def retry_aware_wait(client, path: str, poll: float, timeout: float) -> dict[str, Any]:
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        response = await client.get(path)
        response.raise_for_status()
        body = response.json()
        job = body.get("data", body)
        status = str(job.get("status", job.get("state", ""))).lower()
        attempts = int(job.get("attempts", 0) or 0)
        max_attempts = int(job.get("max_attempts", 3) or 3)
        terminal = {"completed", "processed", "error", "blocked", "dead", "dead_letter"}
        if status in terminal or (status == "failed" and attempts >= max_attempts):
            return job
        if asyncio.get_running_loop().time() >= deadline:
            return {**job, "status": "poll_timeout"}
        await asyncio.sleep(poll)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--phase", choices=sorted(replay_v2.FEATURES), required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--fixture", default=str(replay_v2.DEFAULT_FIXTURE))
    parser.add_argument("--preflight-only", action="store_true")
    parser.add_argument("--delay-seconds", type=float, default=0.0)
    parser.add_argument("--poll-seconds", type=float, default=2.0)
    parser.add_argument("--job-timeout", type=float, default=420.0)
    parser.add_argument("--request-timeout", type=float, default=30.0)
    parser.add_argument("--fail-fast", action="store_true")
    return parser.parse_args()


async def main(args: argparse.Namespace) -> int:
    original_load = replay_v2.load_fixture

    def load_retry_fixture(path: Path) -> dict[str, Any]:
        return scoped_fixture(original_load(path), args.run_id)

    replay_v2.load_fixture = load_retry_fixture
    replay_v2.wait_job = retry_aware_wait
    args.wait = not args.preflight_only
    return await replay_v2.replay(args)


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main(parse_args())))
