# Celery dispatch event-loop repair - 2026-08-21

Status: **repair retained; LOW baseline not accepted**.

The synchronous Celery dispatcher is now invoked outside the FastAPI event-loop thread. Durable
job creation, task identity, queue routing, dispatch failure handling, response semantics, broker
timeouts/retries, extraction, and retrieval behavior remain unchanged.

Focused memory/job lifecycle tests passed 20/20, scale-harness tests passed 8/8, and FAST passed
8/8 before and after LOW. All five post-load integration suites passed. The first integration
attempt was contaminated by the deterministic scale-provider environment in four mocked provider
failure tests; rerunning that suite without the host override passed and confirmed harness/config
drift rather than a product failure.

## LOW result

- Completed iterations: 1,068 of 1,200 scheduled (approximately 132 dropped arrivals)
- API error rate: 0.281% (3 errors)
- Jobs: 469/469 completed, zero retries
- Outbox: 507/507 converged
- Correctness invariants: all passed
- Dispatch failures, PostgreSQL exhaustion, Redis timeout errors: zero
- Provider cost: zero; holdout not accessed

The confirmed 58.15-second add-route stall was removed: maximum add-route time fell to 5.89
seconds. However, the frozen LOW latency gates still failed:

- add route p50/p95/p99: 46.3 / 1,967.59 / 3,759.85 ms;
- retrieval route p50/p95/p99: 47.4 / 3,405.66 / 4,936.9 ms;
- retrieval-core p50/p95/p99: 28.37 / 2,279.37 / 3,972.96 ms;
- job completion p50/p95/p99: 894.17 / 6,427.85 / 12,015.53 ms.

The k6 machine-readable summary was not written because its run directory was missing at summary
time. Server-side phase telemetry, database snapshot, invariant audit, and gate artifacts were
preserved. This is harness drift and prevents treating this execution as an accepted baseline.

The dispatch repair is retained because its direct failure disappeared and all correctness gates
passed. MODERATE must not start. The next work should be diagnosis only of retrieval-core latency
and queue/job tail behavior under LOW, with the k6 artifact directory created before execution.
