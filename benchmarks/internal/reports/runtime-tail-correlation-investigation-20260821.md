# Runtime Tail Correlation Investigation — 2026-08-21

## Outcome

The benchmark-only instrumentation identified the immediate boundary of the rare retrieval tail. The largest request spent 3,827.18 ms in the context segment while retrieval core took 13.21 ms. An independent event-loop sampler recorded 3,815 ms of loop lag within 13.364 ms of that request record.

This confirms a process-level event-loop pause. It does not yet distinguish synchronous blocking inside the process from host/process descheduling.

## Excluded causes for the largest event

- Python GC pause: 5.235 ms, far below the 3.8-second stall.
- API-process CPU saturation: 10.8% in the correlated sample.
- Container CPU throttling: zero events and zero throttled microseconds.
- Retrieval core: 13.21 ms.
- Tokenizer first-use initialization: the preceding preload experiment did not remove the same failure shape.

## Frozen diagnostic result

- Workload: 3 minutes, 2 RPS, deterministic providers.
- Completed/dropped: 359/2.
- API and HTTP error rates: 0%.
- Add p50/p95/p99/max: 70 / 313.8 / 2,893.02 / 3,753 ms.
- Retrieval p50/p95/p99/max: 58.5 / 180.8 / 414.11 / 3,899 ms.
- Job p50/p95/p99/max: 733 / 1,733.4 / 3,346.4 / 4,195 ms.
- Jobs: 159/159 completed, zero retries.
- Outbox: 167/167 converged.
- Correctness audit: all six invariants passed.
- Provider cost: $0.
- Holdout: not accessed.

The run crossed only the add p99 latency threshold. This is a performance failure, not a correctness or API-error failure.

## Regression gates

- Instrumentation/analyzer tests: 14/14 passed.
- FAST before: 8/8 suites passed.
- FAST after: 8/8 suites passed.

## Decision and next experiment

Retain the telemetry only as an explicitly enabled, dedicated-benchmark facility; it remains off by default and cannot activate under production configuration.

The next isolated investigation should add process/thread CPU-time measurement around context construction and an independent host-side heartbeat. Wall time with near-zero CPU time would indicate descheduling; matching wall and CPU time would identify synchronous computation. No production optimization is justified before that distinction is measured.
