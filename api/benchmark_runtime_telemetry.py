from __future__ import annotations

import asyncio
import gc
import json
import logging
import os
import time
from pathlib import Path
from typing import Any

import psutil


LOGGER = logging.getLogger("memoryos.benchmark.runtime")


def runtime_telemetry_enabled(*, app_env: str) -> bool:
    return (
        app_env == "benchmark"
        and os.getenv("MEMORYOS_SCALE_DEDICATED") == "1"
        and os.getenv("BENCHMARK_RUNTIME_TELEMETRY") == "1"
    )


def _cgroup_cpu() -> dict[str, int]:
    for path in (Path("/sys/fs/cgroup/cpu.stat"), Path("/sys/fs/cgroup/cpu/cpu.stat")):
        try:
            values: dict[str, int] = {}
            for line in path.read_text(encoding="utf-8").splitlines():
                key, value = line.split(maxsplit=1)
                values[key] = int(value)
            return values
        except (FileNotFoundError, OSError, ValueError):
            continue
    return {}


class BenchmarkRuntimeTelemetry:
    """Benchmark-only runtime pause sampler; never enabled by normal configuration."""

    def __init__(self, *, interval_seconds: float = 0.05, report_seconds: float = 1.0) -> None:
        self.interval_seconds = interval_seconds
        self.report_seconds = report_seconds
        self._task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()
        self._gc_started: dict[int, float] = {}
        self._gc_durations_ms: list[float] = []
        self._process = psutil.Process()

    async def start(self) -> None:
        if self._task is not None:
            return
        self._stop.clear()
        self._process.cpu_percent(interval=None)
        gc.callbacks.append(self._on_gc)
        self._task = asyncio.create_task(self._sample(), name="benchmark-runtime-telemetry")

    async def stop(self) -> None:
        self._stop.set()
        task, self._task = self._task, None
        if task is not None:
            await task
        while self._on_gc in gc.callbacks:
            gc.callbacks.remove(self._on_gc)

    def _on_gc(self, phase: str, info: dict[str, Any]) -> None:
        generation = int(info.get("generation", -1))
        if phase == "start":
            self._gc_started[generation] = time.perf_counter()
        elif phase == "stop":
            started = self._gc_started.pop(generation, None)
            if started is not None:
                self._gc_durations_ms.append((time.perf_counter() - started) * 1000)

    async def _sample(self) -> None:
        loop = asyncio.get_running_loop()
        expected = loop.time() + self.interval_seconds
        report_at = loop.time() + self.report_seconds
        lags_ms: list[float] = []
        previous_cgroup = _cgroup_cpu()
        while not self._stop.is_set():
            await asyncio.sleep(max(0.0, expected - loop.time()))
            now = loop.time()
            lags_ms.append(max(0.0, (now - expected) * 1000))
            expected += self.interval_seconds
            if now < report_at:
                continue

            current_cgroup = _cgroup_cpu()
            memory = self._process.memory_info()
            payload = {
                "event": "benchmark_runtime_sample",
                "timestamp_unix_ms": round(time.time() * 1000, 3),
                "event_loop_lag_ms_max": round(max(lags_ms, default=0.0), 3),
                "event_loop_lag_ms_avg": round(sum(lags_ms) / len(lags_ms), 3) if lags_ms else 0.0,
                "gc_pause_ms_max": round(max(self._gc_durations_ms, default=0.0), 3),
                "gc_collections": len(self._gc_durations_ms),
                "process_cpu_percent": self._process.cpu_percent(interval=None),
                "process_rss_bytes": memory.rss,
                "cgroup_nr_throttled_delta": current_cgroup.get("nr_throttled", 0)
                - previous_cgroup.get("nr_throttled", 0),
                "cgroup_throttled_usec_delta": current_cgroup.get("throttled_usec", 0)
                - previous_cgroup.get("throttled_usec", 0),
            }
            LOGGER.warning(json.dumps(payload, sort_keys=True))
            lags_ms.clear()
            self._gc_durations_ms.clear()
            previous_cgroup = current_cgroup
            report_at = now + self.report_seconds
