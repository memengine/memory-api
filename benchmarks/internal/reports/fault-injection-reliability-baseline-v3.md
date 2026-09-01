# Reliability and fault-injection baseline v3

## Scope

Development-only deterministic baseline. The unchanged 13-case integration reliability pack
was extended with 19 frozen scenarios for provider/circuit failures, Redis and Qdrant
degradation, outbox reconciliation, dead-letter diagnostics, temporal catch-up/concurrency,
privacy retry/isolation and tenant deletion. Production behavior was not changed. Holdout
was not accessed. No development service was intentionally stopped.

## Harness cleanup

The legacy v2 runner initially reported 13/13 failures because it invoked system Python,
which did not contain pytest. This was classified as harness drift and repaired by using
`sys.executable`; the unchanged pack then passed 13/13. No production code changed for this
cleanup.

## Results

- Scenarios: 32
- Product-evaluable: 32
- Passed: 32
- Product failures: 0
- Harness errors: 0
- Deterministic reliability success: 100%
- Total isolated execution: 204,511.28 ms
- Mean per scenario: 6,390.98 ms
- Minimum: 758.64 ms
- Maximum: 16,708.72 ms

All measured areas and metrics passed, including transaction rollback/no partial memory,
duplicate source-event protection, single dispatch, dead-letter creation/retry/diagnostics,
single claim winner, provider fallback, circuit recovery, Redis failure tolerance, PostgreSQL
fallback during Qdrant failure, cache-only degraded reads, outbox retry/reconciliation,
temporal catch-up, privacy cache isolation, provenance preservation and tenant isolation.

## Interpretation

The result establishes deterministic contract correctness, not operational outage proof.
The tests inject failures at controlled seams and mostly execute components independently.
They do not measure real acknowledgement races, connection loss while a transaction is in
flight, broker redelivery timing, or recovery time after restarting actual containers.

## No production repair proposed

No covered product failure was confirmed, so changing production behavior is not justified.

## One isolated next experiment

Run a controlled development-only Celery crash/redelivery experiment for one unique source
event:

1. Queue one extraction job with a unique tenant/source event.
2. Pause/kill its assigned worker only after processing begins, before acknowledgement where
   observable.
3. Restart the same worker and allow broker redelivery/watchdog recovery.
4. Validate exactly one logical memory, claim revision, version and vector point; correct job
   terminal state; preserved provenance; no cross-tenant effect.
5. Record recovery time, attempts, redelivery count, duplicate rows, outbox rows and API
   availability.

This experiment must use an isolated benchmark tenant/user and restart only one worker. It
must not stop PostgreSQL, Redis, Qdrant, the API, beat, or unrelated worker queues. If the
acknowledgement boundary cannot be observed deterministically, classify it as an operational
harness limitation rather than forcing a production change.
