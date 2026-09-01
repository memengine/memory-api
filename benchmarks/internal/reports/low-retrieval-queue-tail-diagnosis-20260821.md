# LOW retrieval and queue tail diagnosis - 2026-08-21

Status: **diagnosis complete; no production behavior changed**.

The benchmark-only k6 launcher now creates the run-scoped artifact directory before execution,
validates the run ID, and verifies that k6 wrote its summary. Focused harness tests passed 10/10.

## Diagnostic result

The frozen mixed workload ran for three minutes at two arrivals per second:

- 338 completed iterations and 23 dropped arrivals;
- API errors: 7 (2.071%);
- add p50/p95/p99: 273.5 / 1,254.8 / 14,479.52 ms;
- retrieval p50/p95/p99: 206 / 1,243.1 / 14,464.88 ms;
- client-observed job p50/p95/p99: 1,275 / 3,225 / 14,971.4 ms.

All 121 jobs completed with zero retries. Database job completion p95/p99 was only
1,847.32/2,114.5 ms, all 124 outbox events converged, and every correctness invariant passed.
There was no API restart, OOM kill, dispatch failure, PostgreSQL exhaustion, or Redis pool timeout.

## Confirmed tail boundary

One retrieval route took 16,300.23 ms. Its phases were:

- context construction: 16,256.82 ms;
- retrieval-core: 14.76 ms;
- proxy resolution: 0.64 ms;
- domain context: 3.75 ms;
- clarification: 11.28 ms;
- feedback logging: 12.95 ms.

All other retrieval-core p99 latency was 203.09 ms. Add-route p99 was 654.9 ms and add queue p99
was 135.38 ms. Therefore the previous apparent retrieval/queue/job tail is primarily one event-loop
pause during synchronous context construction, not Qdrant retrieval, Celery dispatch, database job
execution, or outbox lag.

`ContextBuilder._count_tokens()` calls `tiktoken.get_encoding("cl100k_base")` synchronously. The
container created the tokenizer cache during the run, and subsequent calls were fast. This makes
lazy tokenizer initialization/cache loading the strongest causal explanation. It should be tested
as one isolated repair before changing retrieval or worker capacity.

## Proposed isolated experiment

Preload and cache the `cl100k_base` encoder before API readiness. Keep token counting, context
selection, rendering, retrieval ranking, Qdrant behavior, and all memory semantics unchanged.

Acceptance for the same three-minute diagnostic:

- zero context-construction stalls above 250 ms;
- API error rate at or below 0.5% and zero EOF responses;
- retrieval p95 below 750 ms and p99 below 1,500 ms;
- add p95 below 500 ms and p99 below 1,000 ms;
- zero unfinished jobs and complete outbox convergence;
- all correctness gates remain green.

Wait for approval before implementing this production initialization change.
