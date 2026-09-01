from __future__ import annotations

import argparse
import asyncio
import json
import os
import time
from collections import Counter
from pathlib import Path

import httpx


async def run_probe(*, base_url: str, api_key: str, concurrency: int, timeout_seconds: float) -> dict:
    if os.getenv("MEMORYOS_SCALE_DEDICATED") != "1":
        raise RuntimeError("Cancellation probe requires the dedicated benchmark marker.")
    if not api_key:
        raise RuntimeError("Benchmark API key is required.")
    statuses: Counter[str] = Counter()
    started = time.perf_counter()

    async with httpx.AsyncClient(timeout=timeout_seconds) as client:
        async def request(index: int) -> None:
            try:
                response = await client.get(
                    f"{base_url.rstrip('/')}/v1/memories/jobs/00000000-0000-0000-0000-{index:012d}",
                    headers={"Authorization": f"ApiKey {api_key}"},
                )
                statuses[f"http_{response.status_code}"] += 1
            except httpx.TimeoutException:
                statuses["client_timeout"] += 1
            except httpx.HTTPError as exc:
                statuses[type(exc).__name__] += 1

        await asyncio.gather(*(request(index + 1) for index in range(concurrency)))

    return {
        "schema_version": "1.0",
        "holdout_used": False,
        "concurrency": concurrency,
        "client_timeout_seconds": timeout_seconds,
        "elapsed_seconds": round(time.perf_counter() - started, 3),
        "outcomes": dict(statuses),
        "provider_calls": 0,
        "provider_cost_usd": 0.0,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:18000")
    parser.add_argument("--concurrency", type=int, default=100)
    parser.add_argument("--timeout-seconds", type=float, default=0.05)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    payload = asyncio.run(run_probe(
        base_url=args.base_url,
        api_key=os.getenv("BENCHMARK_API_KEY", ""),
        concurrency=args.concurrency,
        timeout_seconds=args.timeout_seconds,
    ))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(json.dumps(payload, indent=2))


if __name__ == "__main__":
    main()
