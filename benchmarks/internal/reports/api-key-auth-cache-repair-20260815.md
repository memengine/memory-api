# API-key authentication cache repair — 2026-08-15

Status: **repair retained; LOW baseline not accepted**.

## Isolated change

The existing Redis authentication payload now stores the full SHA-256 fingerprint of the supplied
API key. Cache hits compare that fingerprint using `hmac.compare_digest`; database/cache misses
continue to use bcrypt. Legacy entries without a fingerprint fail closed to a database
revalidation. TTL, revocation window, permissions, Redis fallback, API-key issuance, database
hashes, and authentication responses are unchanged.

Focused authentication tests passed 10/10. They prove a second authenticated request does not
perform a second bcrypt verification and that a legacy cache entry is revalidated.

## Performance

The clean warm diagnostic passed with zero errors or dropped arrivals. Authentication improved
from the prior 220.05/326.93/434.72 ms p50/p95/p99 to 1.96/3.46/5.22 ms. Add p95 was 110.55 ms
and retrieval p95 was 88.30 ms.

The complete frozen LOW workload did not pass overall:

| Metric | Result |
|---|---:|
| Completed / dropped iterations | 1,046 / 154 |
| API error rate | 2.581% |
| HTTP failure rate | 1.228% |
| Auth p50 / p95 / p99 | 1.58 / 33.66 / 677.01 ms |
| Add p50 / p95 / p99 | 60 / 4,031 / 30,002 ms |
| Retrieval p50 / p95 / p99 | 56 / 4,457.6 / 30,002 ms |
| Job p50 / p95 / p99 | 1,159 / 6,422 / 9,703.75 ms |

The auth p95 target remained satisfied under LOW, but p99 contention and the shared request tails
remain. PostgreSQL recorded no connection exhaustion or pool timeout. Two Redis SCAN command
timeouts appeared during the workload; no circuit, preflight, or pool failure was confirmed.

## Correctness and decision

All 431 jobs completed with zero retries; all 457 outbox rows converged. Single-winner alignment,
idempotency, provenance, version chains, and outbox invariants all passed. FAST passed 8/8. The
fully evaluated integration suites passed: fault injection 32/32, integration reliability 13/13,
governance 39/39, lifecycle 14/14, and temporal 18/18. The aggregate orchestrator exceeded its
15-minute wrapper timeout after four suites, so temporal was run separately; this is harness
runtime drift, not a product failure.

Retain the isolated auth repair because it materially removes repeated bcrypt cost with no auth or
correctness regression. Do not accept LOW or start MODERATE: API error rate, dropped arrivals, and
add/retrieval tails still fail the frozen thresholds. The next investigation should isolate the
remaining shared tail/timeout boundary without changing authentication again.
