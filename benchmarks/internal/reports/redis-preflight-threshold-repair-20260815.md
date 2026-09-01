# Redis preflight circuit-threshold repair — 2026-08-15

Status: **repair retained; LOW baseline not accepted**.

## Change

A failed Redis TCP preflight now calls normal circuit failure accounting instead of immediately
calling `force_open()`. The configured five-failure/ten-second threshold and 30-second recovery
behavior remain unchanged. Redis commands, deadlines, retries, fallback semantics, cache behavior,
authentication, and business logic were not changed.

Focused tests passed 20/20, including proof that one failed preflight leaves the circuit closed and
repeated failures open it exactly at the configured threshold. FAST passed 8/8 before and after
load. Fault injection passed 32/32, including Redis degradation and circuit recovery.

## Results

The 2 RPS diagnostic had zero API/HTTP errors and zero circuit-open fallbacks. Add/retrieval/job
p95 were 100/75.8/1454 ms.

The frozen ten-minute LOW run improved materially over the diagnosis baseline:

| Metric | Before | After |
|---|---:|---:|
| Completed iterations | 809 | 1,195 |
| Dropped iterations | 392 | 6 |
| API error rate | 6.922% | 0.084% |
| Add p95 | 30.002 s | 452.7 ms |
| Retrieval p95 | 30.002 s | 454.8 ms |
| Job p95 | 3.875 s | 5.077 s |
| Circuit-open fallbacks | 1,993 | 1,020 |
| Bcrypt fallbacks | 196 | 92 |

All 510 jobs completed with zero retries, all 552 outbox events converged, and every durability
invariant passed. Provider cost was zero and holdout was not used.

## Decision and residual weakness

Retain the repair: it removed the catastrophic p95 timeout cascade, met the <=0.5% error target,
preserved real-outage recovery, and introduced no correctness regression.

Do not accept LOW yet. Frozen p99 limits still failed: add p99 was 2.702 s versus 1.0 s, and
retrieval p99 was 2.821 s versus 1.5 s.

Two actual preflight probes failed. Because a negative probe result is cached for 250 ms, multiple
concurrent requests can each count the same cached negative result toward the circuit threshold.
That can still open the circuit from fewer than five independent probes and caused the remaining
fallback/bcrypt burst. This should be the next isolated investigation; do not combine it with
bcrypt thread offloading or timeout changes.
