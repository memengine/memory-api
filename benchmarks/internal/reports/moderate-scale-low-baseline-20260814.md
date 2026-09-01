# Moderate Scale LOW Baseline — 2026-08-14

Status: **failed performance/reliability acceptance; correctness invariants passed**.

The final run used the disposable `memoryos-scale` project for 10 minutes at 2 iterations/s,
with 5 preallocated VUs and a hard maximum of 10. The deterministic provider was active, paid
provider credentials and holdout files were absent, and real provider cost was $0.

## Results

- 982 of 1,200 expected iterations completed; 219 were dropped after the 10-VU cap saturated.
- Logical API error rate was 2.04%; HTTP request failure rate was 1.75%.
- Add acknowledgement p50/p95/max: 2,927 / 5,259 / 30,009 ms.
- Retrieval p50/p95/max: 2,289 / 5,083 / 30,002 ms.
- Client-observed job completion p50/p95/max: 4,967 / 8,428 / 13,679 ms.
- Database job completion p50/p95/p99: 1,071 / 4,087 / 4,916 ms.
- Queue wait p50/p95/p99: 62 / 718 / 1,386 ms. A 60.9-second maximum was an outlier.
- 418/431 jobs completed; 5 remained queued and 8 processing after a 60-second drain.
- All 476 outbox records converged.

Correctness audit passed: one winning revision, winner alignment, event idempotency, provenance,
version chains, and outbox convergence. FAST passed 8/8 both before and after LOW.

## Confirmed weakness

The single API instance became the limiting boundary at this LOW arrival rate. Logs show Redis
circuit-breaker timeouts propagating through request middleware as HTTP 500 responses, followed by
client timeouts and VU saturation. This reduced achieved throughput to 1.61 iterations/s and left
13 jobs unfinished after the drain window. No production optimization was made.

## Harness findings

Three benchmark-environment defects were found and repaired before the final run: a missing import
in the fixture branch, a 128-vs-1536 Qdrant dimension mismatch, and a missing embedding-model
provenance row. Their contaminated disposable states were destroyed and were not included in the
final baseline.

Run-scoped cleanup removed 21 proxy users, 398 Qdrant points, and every associated memory, claim,
revision, job, source event, and outbox record. The disposable Compose project was then destroyed;
the persistent development project remained running.
