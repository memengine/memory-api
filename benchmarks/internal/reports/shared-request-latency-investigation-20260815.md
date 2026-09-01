# Shared Add/Retrieval Latency Investigation — 2026-08-15

Status: diagnosis complete; no production authentication behavior changed.

## Method

Benchmark-only timings measured each component's own work rather than nested middleware duration. The disposable 1 req/s run covered both add and retrieval traffic. Holdout and paid providers were not used.

## Results

| Shared phase | p50 | p95 | p99 |
|---|---:|---:|---:|
| API-key authentication | 220.05 ms | 326.93 ms | 434.72 ms |
| Region resolution | 1.60 ms | 232.79 ms | 282.73 ms |
| Quota response envelope | 1.27 ms | 4.37 ms | 7.30 ms |
| Webhook session-factory construction | 0.37 ms | 1.11 ms | 3.65 ms |
| Add route body | 22.26 ms | 50.57 ms | 233.93 ms |

API-key authentication is the dominant shared median and tail contributor. The Redis cache key is derived from the raw key fingerprint, but a cache hit stores the bcrypt hash and calls `bcrypt.checkpw()` again on every request. That CPU-hard verification measured approximately 220 ms median, defeating most latency benefit of the cache.

Region resolution has a separate Redis/cache tail and should be investigated later. Quota response and eager webhook factory construction are not material latency boundaries in this run, although eager factory allocation remains an allocation issue.

Two retrieval requests ended with transient EOF while the API container remained healthy; they are recorded separately and do not explain the phase distribution.

## One isolated proposed repair

Add the full SHA-256 API-key fingerprint to the existing Redis auth-cache payload. On cache hit, compare the supplied key's full fingerprint using `hmac.compare_digest`. Continue using bcrypt against the database hash on every cache miss. Treat legacy cache entries without the full fingerprint as misses so they are safely revalidated and refreshed.

This retains the current five-minute cache/revocation window and tenant/user/API-key payload. It does not alter database hashes, API-key issuance, authorization rules, Redis TTL, failure fallback, or authentication response semantics.

Acceptance:

- cached API-key auth p50 <=10 ms and p95 <=50 ms;
- cache misses and legacy entries still require bcrypt verification;
- incorrect keys and fingerprint mismatches are rejected;
- add and retrieval p95 improve materially under the frozen LOW workload;
- zero cross-tenant/user/agent leakage and no auth regression;
- API errors <=0.5%, zero unfinished jobs, complete outbox convergence;
- PostgreSQL peak <=50, all durability invariants and FAST/security/integration gates green;
- no holdout/provider cost.

