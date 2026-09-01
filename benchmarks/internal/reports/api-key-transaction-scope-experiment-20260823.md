# API-key transaction-scope experiment — 2026-08-23

Status: failed frozen acceptance and reverted. Production authentication behavior is unchanged.

## Isolated change tested

Candidate API-key identity/hash data was read in a short session, bcrypt ran outside the session, and `last_used_at` was written in a second short transaction. Lookup fallback, bcrypt, permissions, cache, and key-selection semantics were unchanged.

## Frozen MODERATE comparison

| Metric | Reference | Candidate | Change |
|---|---:|---:|---:|
| Completed iterations | 1,617 | 1,654 | +2.3% |
| Dropped iterations | 7,964 | 7,946 | -0.2% |
| API error rate | 26.96% | 17.95% | improved 9.01 pp |
| HTTP failure rate | 43.27% | 35.59% | improved 7.68 pp |
| Add p50 / p95 | 21.224 / 30.001 s | 19.926 / 30.001 s | p50 improved; p95 unchanged |
| Retrieval p50 / p95 | 24.071 / 30.002 s | 23.290 / 30.001 s | slight improvement |
| Job p50 / p95 | 31.114 / 54.136 s | 33.762 / 51.558 s | mixed |
| DB queue-wait p95 | 28.819 s | 8.808 s | materially improved |
| DB job-completion p95 | 38.260 s | 15.440 s | materially improved |
| Peak PostgreSQL connections | 98 | 75 | passed `<80` target |
| Rejected observer connections | 8 | 0 | exhaustion removed in this run |

## Failed acceptance

The authentication/global engine still accumulated long-lived transactions:

- authentication-associated transaction age reached 484.908 seconds;
- persistent `BEGIN` idle-in-transaction observations remained;
- maximum idle-in-transaction sessions remained 35;
- overall API and latency thresholds still failed.

The change reduced pressure but did not remove the confirmed cancellation/session-lifetime leak. It therefore failed the requirements of no authentication transaction older than five seconds and zero persistent authentication idle-in-transaction sessions after drain.

## Correctness

- 671/671 jobs completed after drain.
- 681/681 outbox events converged.
- Single winner, winner alignment, event idempotency, provenance, version-chain integrity, and outbox correctness all passed.
- Deterministic provider cost: zero; holdout excluded.

## Decision

The candidate was reverted. The next investigation should isolate cancellation-safe cleanup of authentication sessions under client timeout/event-loop overload, including whether `BaseHTTPMiddleware` cancellation prevents `AsyncSession.__aexit__` rollback/check-in. Do not combine this with clarification-queue claiming or another performance optimization.
