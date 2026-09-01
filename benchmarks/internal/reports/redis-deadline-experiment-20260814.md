# Redis Deadline Experiment - 2026-08-14

The disposable benchmark stack ran the same three-minute, 2 arrival/s mixed workload for each
candidate. The deterministic provider was active, holdout access was disabled, and every run used
a 60-second drain. PostgreSQL session reuse, enum correction, and the Redis TCP preflight remained
unchanged. No production timeout default was changed.

| Candidate (connect / command / circuit) | Iterations | Dropped | API errors | HTTP 500 | Redis timeouts: circuit / pool / TCP | Redis guarded p50 / p95 / p99 | Add p95 / p99 | Retrieval p95 / p99 | Job p95 / p99 | Unfinished | Fallbacks | Correctness |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|
| Control (100 / 100 / 200 ms) | 334 | 27 | 1.198% | 5 | 6 / 1 / 17 | 2.6 / 298.5 / 298.5 ms | 5,199.5 / 8,124.5 ms | 5,268.8 / 7,995.9 ms | 8,277.8 / 9,739.1 ms | 0 | 5,300 | pass |
| B (250 / 250 / 500 ms) | 351 | 10 | 0.285% | 0 | 0 / 0 / 10 | 0.8 / 3.1 / 3.4 ms | 3,374.0 / 7,810.8 ms | 2,289.5 / 3,914.7 ms | 4,395.7 / 9,335.6 ms | 0 | 5,334 | pass |
| C (500 / 500 / 750 ms) | 354 | 6 | 0% | 0 | 0 / 0 / 14 | 0.6 / 4.8 / 9.2 ms | 2,960.2 / 4,613.2 ms | 3,025.7 / 4,300.7 ms | 4,520.1 / 5,860.0 ms | 0 | 5,045 | pass |

Pool acquisition p50/p95/p99 was 0.107/13.799/13.799 ms for control,
0.078/0.103/0.103 ms for B, and 0.062/0.135/0.135 ms for C. TCP preflight
p50/p95/p99 was 234.850/406.907/406.907 ms, 2.035/257.298/276.158 ms, and
225.561/273.695/273.695 ms respectively.

Instrumentation records every failure and samples 10% of successful low-level events. Redis-py's
execution path did not emit separate completed connection-establishment or command-completion
events in these runs. Therefore those two distributions are unavailable rather than reported as
zero; pool acquisition and the full circuit-guarded Redis operation are reported separately.

Candidate C is the evidence-supported choice. Relative to control it removed all observed circuit
and pool timeouts, API errors, and HTTP 500 responses; reduced dropped arrivals by 77.8%; completed
20 more iterations; and improved add, retrieval, and job tail latency. It did not hide TCP preflight
failures: 14 remained explicitly recorded, and all correctness invariants passed.

Recommended production change for separate approval: change Redis connection and command timeouts
from 100 ms to 500 ms and the circuit execution deadline from 200 ms to 750 ms, leaving the TCP
preflight and Redis semantics/retries unchanged. Before rollout, apply through configuration and
retain the same telemetry so real Redis outages remain observable. This recommendation has not been
applied.
