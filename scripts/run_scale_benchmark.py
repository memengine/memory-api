from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import threading
import time
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "moderate_scale_k6.js"
RUN_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
REQUIRED_SCALE_SERVICES = {
    "api",
    "celery-scale",
    "celery-background",
    "celery-beat",
    "postgres",
    "redis",
    "qdrant",
}
HEALTH_REQUIRED_SERVICES = {"api", "postgres", "redis", "qdrant"}


def require_disposable_stack_ready(environment: dict[str, str]) -> None:
    if environment.get("MEMORYOS_SCALE_DEDICATED") != "1":
        raise RuntimeError("Disposable scale marker is required before checking services.")
    command = [
        "docker", "compose", "-p", "memoryos-scale",
        "-f", "docker-compose.scale.yml", "--env-file", ".env.scale",
        "ps", "--format", "json",
    ]
    completed = subprocess.run(command, cwd=ROOT, env=environment, capture_output=True, text=True, check=False)
    if completed.returncode != 0:
        raise RuntimeError("Unable to verify disposable benchmark services.")
    raw = completed.stdout.strip()
    try:
        parsed = json.loads(raw)
        rows = parsed if isinstance(parsed, list) else [parsed]
    except json.JSONDecodeError:
        rows = [json.loads(line) for line in raw.splitlines() if line.strip()]
    services = {str(row.get("Service", "")): row for row in rows}
    missing = sorted(REQUIRED_SCALE_SERVICES - services.keys())
    if missing:
        raise RuntimeError(f"Disposable benchmark services missing: {', '.join(missing)}")
    not_running = sorted(
        name for name, row in services.items()
        if name in REQUIRED_SCALE_SERVICES and str(row.get("State", "")).lower() != "running"
    )
    if not_running:
        raise RuntimeError(f"Disposable benchmark services not running: {', '.join(not_running)}")
    unhealthy = sorted(
        name for name in HEALTH_REQUIRED_SERVICES
        if str(services[name].get("Health", "")).lower() != "healthy"
    )
    if unhealthy:
        raise RuntimeError(f"Disposable benchmark services not healthy: {', '.join(unhealthy)}")


class HostHeartbeat:
    def __init__(self, *, interval_seconds: float = 0.05, anomaly_seconds: float = 0.1) -> None:
        self.interval_seconds = interval_seconds
        self.anomaly_seconds = anomaly_seconds
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.samples = 0
        self.max_lag_ms = 0.0
        self.anomalies: list[dict[str, float]] = []

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="scale-host-heartbeat", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2)

    def _run(self) -> None:
        expected = time.perf_counter() + self.interval_seconds
        while not self._stop.wait(max(0.0, expected - time.perf_counter())):
            now = time.perf_counter()
            lag_ms = max(0.0, (now - expected) * 1000)
            self.samples += 1
            self.max_lag_ms = max(self.max_lag_ms, lag_ms)
            if lag_ms >= self.anomaly_seconds * 1000:
                self.anomalies.append({"timestamp_unix_ms": time.time() * 1000, "lag_ms": round(lag_ms, 3)})
                expected = now + self.interval_seconds
            else:
                expected += self.interval_seconds

    def write(self, path: Path) -> None:
        payload = {
            "sample_interval_ms": self.interval_seconds * 1000,
            "anomaly_threshold_ms": self.anomaly_seconds * 1000,
            "samples": self.samples,
            "max_lag_ms": round(self.max_lag_ms, 3),
            "anomalies": self.anomalies,
        }
        path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def prepare_summary_path(run_id: str, stage: str) -> Path:
    if not RUN_ID_PATTERN.fullmatch(run_id):
        raise ValueError("run_id must contain only letters, numbers, dot, underscore, and hyphen")
    output = ROOT / "artifacts" / "internal-benchmarks" / "scale" / run_id / f"k6-{stage.lower()}.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    return output


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--stage", default="LOW")
    args = parser.parse_args()

    stage = args.stage.strip().upper()
    output = prepare_summary_path(args.run_id, stage)
    k6 = shutil.which("k6")
    if not k6:
        raise RuntimeError("k6 is not available on PATH")

    environment = os.environ.copy()
    environment["RUN_ID"] = args.run_id
    environment["SCALE_STAGE"] = stage
    environment["K6_SUMMARY_PATH"] = str(output.relative_to(ROOT)).replace("\\", "/")
    require_disposable_stack_ready(environment)
    heartbeat = HostHeartbeat() if environment.get("BENCHMARK_HOST_HEARTBEAT") == "1" else None
    if heartbeat is not None:
        heartbeat.start()
    try:
        completed = subprocess.run([k6, "run", str(SCRIPT)], cwd=ROOT, env=environment, check=False)
    finally:
        if heartbeat is not None:
            heartbeat.stop()
            heartbeat.write(output.parent / "host-heartbeat.json")
    if not output.exists():
        raise RuntimeError(f"k6 did not write the expected summary artifact: {output}")
    return completed.returncode


if __name__ == "__main__":
    raise SystemExit(main())
