# Reliability and fault-injection architecture inventory

## Existing recovery surfaces

- Extraction jobs persist attempts, maximum attempts, processing timestamps, errors and
  dead-letter state. The watchdog guardedly requeues stale processing jobs once.
- Tenant request idempotency is Redis-scoped; durable logical source events have PostgreSQL
  uniqueness and concurrent duplicate protection.
- Conversation creation and memory/claim/version/outbox writes use explicit transactions;
  pipeline failure rolls back memory state and marks the conversation failed separately.
- Vector writes use a transactional outbox with atomic claiming, three-attempt terminal
  failure and database/Qdrant reconciliation.
- Claim rows are locked during winner changes and PostgreSQL enforces one activated revision.
- Provider routing uses per-provider circuit breakers and fallback. Retrieval supports
  PostgreSQL fallback when Qdrant is unavailable and cache-only degraded mode.
- Temporal transition scans overdue rows, uses row locks/skip-locked, isolates tenant
  failures, and catches up after missed schedules.
- Hard/privacy deletion removes memory/claim state, enqueues vector deletion and invalidates
  identity-scoped Redis and local retrieval caches.

## Existing baseline and extension

The unchanged 13-case integration-reliability pack remains the base. A frozen 19-case
development extension adds provider failure, circuit recovery, Redis/Qdrant degradation,
outbox reconciliation, dead-letter diagnostics, temporal catch-up/concurrency, privacy cache
isolation and tenant deletion. No services are intentionally stopped during this baseline;
failures are injected deterministically through existing test seams. Heavy load is excluded.

## Known coverage limits

This composed baseline does not kill a real worker between database commit and Celery
acknowledgement, interrupt a live PostgreSQL transaction at the network layer, or measure
multi-minute recovery under actual infrastructure outages. Those destructive/operational
experiments require a separate controlled window after the deterministic baseline.
