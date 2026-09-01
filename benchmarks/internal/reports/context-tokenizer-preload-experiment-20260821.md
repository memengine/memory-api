# Context Tokenizer Preload Experiment — 2026-08-21

## Decision

The isolated startup-preload experiment failed its primary acceptance criterion and was reverted. The benchmark launcher and diagnostic artifacts remain; normal production context-building behavior is unchanged.

## Result

The final frozen 3-minute LOW diagnostic completed 357 iterations at 2 RPS with four dropped arrivals, zero API errors, 161/161 jobs completed, 170/170 outbox events processed, and all correctness invariants passing. Provider cost was $0.

Typical latency improved, but the tail did not: retrieval p95 was 138 ms and p99 was 4,721.9 ms. Context construction measured p95 0.48 ms and p99 1.18 ms, yet one request stalled there for 5,926.06 ms. Performing a representative tokenizer encode during application startup therefore did not eliminate stalls above the required 250 ms ceiling.

## Interpretation

Tokenizer lazy initialization is not the sole cause. The rare pause is more consistent with event-loop/process scheduling, Python garbage collection, or host/container CPU scheduling occurring while the synchronous context segment is timed. This is a diagnosis, not yet a confirmed root cause.

## Regression status

- Focused context-builder and scale-harness tests: 19/19 passed after revert.
- Consolidated post-revert FAST gate: 8/8 suites passed; no product failures or harness errors.
- Holdout was not accessed.

## Next isolated investigation

Add benchmark-only correlation telemetry for event-loop lag, Python GC pauses, process CPU usage, and container throttling. Do not change production latency behavior until that evidence identifies the actual boundary.
