# API-key authentication cache/circuit diagnosis

Date: 2026-08-22  
Scope: frozen MODERATE run `moderate-webhook-repair-valid-20260822`  
Status: diagnosis only; no production behavior changed; holdout not used

## Outcome

The confirmed bottleneck is an authentication-local Redis deadline that conflicts with the accepted central Redis deadlines.

`AuthMiddleware._authenticate_api_key` wraps both cache GET and cache SET in `asyncio.wait_for(..., timeout=0.2)`. The Redis client and shared Redis circuit now use 500 ms socket/command timeouts and a 750 ms circuit execution deadline. Under request-side CPU/event-loop pressure, the 200 ms authentication wrapper expires first and calls `force_open()`. The shared Redis circuit then remains open for 30 seconds. During that interval:

1. API-key GET falls back to a cache miss.
2. PostgreSQL lookup, `last_used_at` commit, and bcrypt verification run again.
3. The subsequent cache SET also falls back while the circuit is open, so the cache cannot heal.
4. Repeated DB+bcrypt work increases API pressure and makes another caller-level timeout more likely.

This is a positive feedback loop at the auth/cache/circuit integration boundary, not evidence that Redis itself lacks capacity.

## Frozen-run evidence

| Measurement | Result |
|---|---:|
| API-key cache hits | 280 |
| Cache misses | 2,783 |
| Cache timeouts | 70 |
| Total cache lookups | 3,133 |
| Hit rate | 8.94% |
| PostgreSQL fallbacks | 2,853 |
| bcrypt verifications | 2,853 |
| Redis circuit-open fallback messages | 30,167 |
| DB fallback p50 / p95 / p99 | 1,793 / 7,489 / 12,589 ms |
| bcrypt p50 / p95 / p99 | 333 / 516 / 611 ms |
| API CPU snapshot | 102.28% |
| Redis CPU snapshot | 1.98% |

The first cache timeout occurred at `03:50:32`, immediately followed by circuit-open fallback. The first hit did not occur until `03:53:40`. Hits appeared only in short recovery windows; misses continued through the end of the run. Sampled Redis commands include successful GET/SET operations, confirming that Redis was reachable between circuit-open periods.

The k6 workload sends the same `BENCHMARK_API_KEY` on all authenticated requests. The cache identity is stable (`fingerprint_api_key(raw_key)[:16]`) and the positive entry has a 300-second TTL. Key cardinality or per-request key variation therefore does not explain the misses.

## Boundary findings

- **Cache identity:** stable for this workload; no evidence of accidental key churn.
- **TTL:** 300 seconds; not the cause of misses recurring within seconds.
- **Invalidation:** API-key revocation does not explicitly evict the positive cache entry. This is a separate security/lifecycle weakness and must not be mixed into the performance repair.
- **Redis circuit:** process-local and shared by Redis consumers in the API process; forced open by the auth-local wrapper for 30 seconds.
- **Database fallback:** correct fail-open-for-cache behavior, but expensive because every miss commits `last_used_at` and runs bcrypt.
- **bcrypt:** expected verification cost becomes a capacity problem only because cache misses repeat.
- **Tests:** the existing cache test proves sequential first-miss/second-hit behavior with injected fake Redis. It does not exercise the real shared circuit, conflicting deadlines, recovery, or concurrent warm-up.

## One isolated proposed repair

Remove only the two authentication-local 200 ms `asyncio.wait_for` wrappers around API-key cache GET and SET, and stop force-opening Redis from those caller-level wrapper timeouts. Let the existing Redis socket timeout (500 ms), command timeout (500 ms), and shared circuit execution deadline (750 ms) remain the single timeout authority.

Do not change cache keys, TTL, bcrypt, database fallback, circuit recovery/threshold semantics, Redis retry/fallback behavior, or API-key revocation in this repair.

## Acceptance criteria

Run focused concurrent auth tests, then the unchanged frozen MODERATE workload:

- cache hit rate after a 30-second warm-up: at least 95%
- database fallbacks after warm-up: at most 1% of authenticated requests
- bcrypt verifications after warm-up: at most 1% of authenticated requests
- no caller-level auth timeout force-opens
- Redis-related HTTP 500s: zero
- API error rate: at most 0.5%
- no regression in invalid-key rejection or Redis-unavailable fallback
- revoked-key behavior must not become worse (dedicated invalidation repair remains separate)
- completed/dropped iterations and add/retrieval/job latency must materially improve over this failed run
- all correctness invariants, FAST, and required integration gates remain green

If these criteria fail, revert the repair and investigate cache stampede/single-flight separately rather than combining it into this change.
