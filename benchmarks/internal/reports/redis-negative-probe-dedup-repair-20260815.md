# Redis negative-probe deduplication repair - 2026-08-15

Status: **repair retained; LOW baseline not accepted**.

## Change

The Redis TCP preflight now reports whether its result came from a fresh network probe or the
250 ms probe cache. A failed fresh probe increments circuit failure accounting once. Requests that
reuse the cached negative result still use the existing fallback but do not increment the circuit
again. Independent fresh failures still open the breaker at the unchanged configured threshold.

No timeout, cache TTL, bcrypt, Redis command, recovery, retry, or business behavior changed.

Focused tests passed 21/21. FAST passed 8/8 before and after load. Fault injection passed 32/32,
including Redis degradation and circuit recovery.

## Direct result

Compared with the retained threshold-only run:

- circuit-open fallbacks: 1,020 -> 33;
- auth cache misses: 91 -> 4;
- bcrypt fallbacks: 92 -> 4;
- one actual failed TCP probe no longer produced a large auth fallback storm.

The direct repair therefore passed and is retained. All 452 jobs completed with zero retries, all
489 outbox events converged, and every durability invariant passed. Provider cost was zero and
holdout was not used.

## LOW result and separate remaining failure

- Completed/dropped iterations: 1,127/74
- API error rate: 1.686%
- Add p50/p95/p99: 73 ms / 345.25 ms / 30.002 s
- Retrieval p50/p95/p99: 66 ms / 291 ms / 16.676 s
- Job p50/p95/p99: 1.204 s / 4.126 s / 5.692 s

LOW remains unaccepted, but the failure is no longer the Redis/auth circuit cascade. Instrumented
completed retrieval work had p95/p99 of 139/270 ms and a 363 ms maximum. One add request instead
spent 58.036 s inside `MemoryService.queue_memory_add`; its dominant boundary is synchronous
Celery `send_task()` dispatch executed inside the async FastAPI request. That event-loop stall also
delayed unrelated retrieval clients before their route work could run.

The next isolated experiment should move only Celery broker dispatch off the API event loop while
preserving durable job creation, dispatch failure handling, queue routing, task identity, and
response semantics. Do not mix broker retry or timeout changes into that experiment.
