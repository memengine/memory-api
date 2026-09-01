# Shared request-tail diagnosis — 2026-08-15

Status: **root cause confirmed; no production repair applied**.

## Method

Benchmark-only instrumentation separated API-key cache lookup/database/bcrypt phases and retrieval
proxy, core, context, domain, clarification, feedback, and route phases. The valid frozen LOW run
used the isolated deterministic-provider stack at two arrivals per second for ten minutes. Holdout
and paid providers were not used.

An initial run was excluded as harness drift: `docker compose restart` preserved the Celery pidfile
inside the worker container and the scale worker stopped. Its fixtures were cleaned, the dedicated
Redis broker was cleared, the worker was recreated, and a 2 RPS worker check confirmed normal job
completion before the valid sustained run.

## Confirmed failure chain

The valid LOW run recorded one failed Redis TCP preflight at 215.958 ms. The preflight path calls
`force_open()`, so that single failure opened the shared Redis circuit for 30 seconds even though
there were zero Redis circuit-execution errors. The open circuit generated 1,993 fallback events.

Authentication consequently recorded 189 cache misses, seven cache timeouts, and 196 database
bcrypt verifications. Bcrypt is synchronous in the async request handler and measured
218.56/341.40/387.01 ms p50/p95/p99. The fallback storm serialized CPU work on the API event loop:
database fallback p95 reached 5.273 s, retrieval core reached 96.831 s maximum, and add queueing
reached 96.760 s maximum. This explains why independent add and retrieval requests stalled
together.

The context builder was not the sustained cause. A warm-up check had one 3.84 s context outlier,
but valid LOW context p95/p99 were 0.37/0.84 ms. Retrieval core p95 was 99.16 ms outside the
circuit-open cascade.

## LOW result and correctness

- Completed/dropped iterations: 809/392
- API error rate: 6.922%
- Add p50/p95/p99: 61 ms / 30.002 s / 30.005 s
- Retrieval p50/p95/p99: 57 ms / 30.002 s / 30.005 s
- Job p50/p95/p99: 919 ms / 3.875 s / 21.017 s
- Jobs: 368 completed, zero unfinished/retries
- Outbox: 405 done, zero pending
- All durability invariants passed

## One isolated proposed repair

Replace the TCP preflight branch's immediate `force_open()` with normal Redis circuit failure
accounting (`_record_failure()`). A single false/slow probe would fail only that operation and use
the existing fallback; five failures within the configured ten-second window would still open the
circuit, preserving real-outage protection and the existing 30-second recovery behavior.

Do not combine this with bcrypt thread offloading, retry changes, cache changes, or new timeout
values in the same experiment.

Acceptance for the isolated experiment:

- one injected preflight failure does not open the Redis circuit;
- five qualifying failures still open it and real Redis unavailability still uses fallback;
- circuit-open fallback count under frozen LOW falls materially from 1,993;
- auth database/bcrypt fallback calls remain limited to legitimate cache miss/expiry behavior;
- API error rate <=0.5%, zero Redis-related HTTP 500s;
- add/retrieval p95 no longer reaches the client timeout and improves materially;
- zero unfinished jobs, complete outbox convergence, and all correctness/security gates green.
