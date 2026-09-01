# CPU versus Descheduling Investigation — 2026-08-21

## Outcome

The frozen diagnostic reproduced the tail and separated computation from waiting. The largest retrieval route took 7,016.61 ms, including 6,932.62 ms attributed to context construction. That context interval consumed only 268.92 ms of process CPU and 268.93 ms of thread CPU. Approximately 6,663.69 ms, or 96.12%, was non-CPU wall time.

The in-container event loop paused for 6,921 ms. During the same run, the independent Windows-host heartbeat sampled 3,710 times at 50 ms intervals, recorded no anomaly above 100 ms, and had a maximum lag of 39.999 ms. Python GC contributed 2.306 ms and cgroup CPU throttling remained zero.

## Interpretation

This is not a multi-second synchronous Python computation. It is also not a Windows-host-wide scheduling pause. The remaining boundary is the Docker Desktop/Linux VM or an external/native wait isolated to the API container process. The evidence does not justify a MemoryOS production optimization.

A native Linux or cloud-runner execution of the same frozen workload is the appropriate environmental validation. If the tail disappears, classify it as local Docker Desktop benchmark noise. If it persists, add syscall/native-call tracing around context tokenization before changing behavior.

## Run result

- Completed/dropped iterations: 356/5.
- API and HTTP error rates: 0%.
- Add p50/p95/p99/max: 71 / 791.6 / 6,840.16 / 7,799 ms.
- Retrieval p50/p95/p99/max: 54 / 176.8 / 4,138.32 / 7,060 ms.
- Job p50/p95/p99/max: 625 / 1,993 / 7,469.72 / 7,955 ms.
- Jobs: 133/133 completed with zero retries.
- Outbox: 140/140 converged.
- Correctness: all six invariants passed.
- Provider cost: $0.
- Holdout: not accessed.

The run failed add p95/p99 and retrieval p99 latency thresholds solely because of the reproduced pause. It had no correctness or API-error regression.

## Regression gates

- Focused benchmark/context tests: 24/24 passed.
- FAST before: 8/8 passed.
- FAST after: 8/8 passed.

## Decision

Retain wall/CPU and host-heartbeat instrumentation as benchmark-only diagnostics. They are guarded by the dedicated benchmark environment and remain inactive in normal production/development configuration.

Stop local production tuning for this tail. Record a native-Linux comparison as a future environment-validation task and return to the broader benchmark roadmap.
