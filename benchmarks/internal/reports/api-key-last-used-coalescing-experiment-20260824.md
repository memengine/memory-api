# API-key last-used coalescing experiment — 2026-08-24

Status: intended lock contention removed, but frozen acceptance failed. Candidate reverted; production behavior unchanged.

## Candidate

Only API-key usage timestamp persistence changed:

- API-key lookup and bcrypt verification retained their existing selection and authentication semantics.
- Bcrypt ran after the lookup session closed.
- `last_used_at` used a separate conditional PostgreSQL update.
- The update applied only when the stored timestamp was absent or at least 60 seconds old.
- Redis was not required to coordinate the coalescing decision.

Permissions, cache lookup/set behavior, Redis failure fallback, rate limiting, request authorization, workers, extraction, retrieval, and benchmark traffic were unchanged.

## Frozen MODERATE comparison

| Metric | Reference | Candidate | Result |
|---|---:|---:|---|
| Completed iterations | 1,460 | 1,454 | no improvement |
| Dropped iterations | 8,138 | 8,146 | no improvement |
| API error rate | 62.72% | 32.44% | improved 30.28 pp; still failed |
| HTTP failure rate | 69.98% | 52.70% | improved 17.28 pp; still failed |
| Add p50 / p95 / p99 | 30.001 / 30.011 / 30.051 s | 23.047 / 30.004 / 30.009 s | p50 improved; tail still timed out |
| Retrieval p50 / p95 / p99 | 30.002 / 30.013 / 30.078 s | 26.218 / 30.007 / 30.009 s | p50 improved; tail still timed out |
| Job p50 / p95 / p99 | 43.274 / 57.681 / 58.814 s | 39.136 / 55.223 / 58.784 s | modest improvement |
| DB queue-wait p95 | 50.361 s | 20.080 s | materially improved |
| DB job-completion p95 | 61.019 s | 29.413 s | materially improved |
| Peak PostgreSQL connections | 99 | 99 | failed `<80` |
| Rejected observer connections | 33 | 17 | improved; failed zero target |
| PostgreSQL blocked observations | 2,591 | 0 | passed local goal |

## Coalescing behavior

- Usage-touch attempts: 2,708.
- Durable updates: 21.
- Coalesced no-op updates: 2,687.
- PostgreSQL tuple/transaction-ID lock waits on `api_keys.last_used_at`: zero.
- Usage-touch p50/p95/p99: 1.391 / 3.900 / 5.082 s at the request phase.
- Completed API-key update transaction p50/p95/p99: 0.383 / 1.313 / 2.084 s.

The 21 durable updates across the 20-minute run confirm that usage timestamps continued to refresh approximately once per configured interval. The candidate successfully removed the hot-row lock queue.

## Correctness and drain

- All 633 accepted jobs completed with zero retries or unfinished jobs.
- All 660 outbox events converged.
- Single-winner, winner alignment, idempotency, provenance, version-chain, and outbox correctness checks passed.
- Holdout excluded and deterministic-provider cost was zero.

## Why the candidate failed overall acceptance

Removing the API-key row lock substantially reduced database queueing and API errors but did not remove the initiating authentication/cache failure:

- Cache lookups: 2,637 misses, 71 timeouts, 210 hits.
- Redis circuit-open fallbacks: 2,241.
- Redis command errors: 80.
- Database/bcrypt fallbacks: 2,708.
- Database fallback p50/p95/p99: 3.252 / 8.142 / 9.984 s.

PostgreSQL still reached 99 connections and rejected 17 observer connections. Final state remained 69 idle connections and two `celery-background` idle-in-transaction `BEGIN` sessions aged approximately 67 and 967 seconds.

The coalesced timestamp write was an amplifier repair, not a sufficient root-cause repair. Retaining it would hide one symptom while the frozen MODERATE benchmark still fails its primary capacity criteria, so the candidate was reverted.

## Next isolated diagnosis

Do not attempt another API-key timestamp or PostgreSQL pool change next.

The highest-value next step is a read-only Redis circuit transition diagnosis under MODERATE load. Attribute the first qualifying command failures that open the shared circuit, including command name, latency/deadline type, client role, and whether SCAN-based invalidation is involved. Correlate circuit-open intervals with authentication-cache misses, bcrypt/database fallback, and connection growth.

Do not change Redis timeouts, preflight, circuit thresholds, invalidation mode, authentication, or cache semantics until the initiating command/failure sequence is confirmed.
