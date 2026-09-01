# Redis preflight and circuit correlation diagnosis

Date: 2026-08-22  
Evidence: `moderate-auth-singleflight-20260822`  
Status: diagnosis only; no production behavior changed

## Finding

The dominant remaining Redis-availability failure is the shared TCP connectivity preflight, not
cache SET correctness.

Every Redis circuit call performs `_redis_connectivity_preflight()`. When the 250 ms probe cache
is stale, concurrent callers can all submit independent `socket.create_connection` work to a
single-thread executor. Each caller waits only 100 ms. Requests queued behind another probe can
therefore time out before their probe starts, be recorded as Redis failures, cache an
`unreachable` result, and send all Redis consumers through fallback even while Redis is serving
successful commands.

The authentication-local 200 ms wrapper is a secondary amplifier: its 121 timeouts call
`force_open()` and restart the 30-second circuit recovery interval. It was not the earliest
failure in this run; TCP preflight errors appeared first.

## Evidence

| Observation | Result |
|---|---:|
| Authentication-active seconds | 973 |
| Active seconds containing shared Redis fallback logs | 970 / 973 = 99.7% |
| Active seconds containing an authentication cache hit | 98 / 973 = 10.1% |
| Authentication cache lookups | 13,488 |
| Authentication cache hits | 1,072 = 7.95% |
| Authentication cache timeouts | 121 |
| Database/bcrypt fallback leaders | 728 |
| TCP preflight success samples | 104 |
| TCP preflight errors | 427 |
| Failed probes within 250 ms of successful Redis GET/SET | 291 / 427 = 68.1% |
| Successful sampled Redis GET commands | 1,693 |
| Successful sampled Redis SET commands | 66 |
| Circuit-open/fallback log entries across Redis consumers | 56,945 |

Redis error telemetry separated by boundary:

- TCP preflight failures: 427;
- command timeouts: 156;
- circuit execution timeouts/deadlines: 16;
- pool acquisition timeouts: 4;
- connection timeouts: 4.

The first TCP preflight failures occurred at 12:01:03.988Z and 12:01:03.990Z. The first
authentication-wrapper timeout occurred later at 12:01:04.725Z. Successful cache hits and Redis
commands occurred between and close to reported preflight failures, demonstrating that many probe
failures were not Redis endpoint outages.

The 66 sampled successful SET commands prove cache fill can work. Minutes with database fallback
but no sampled SET align with the shared circuit/preflight fallback path, where `_cache_api_key_auth`
returns through `on_redis_open(None)` without attempting SET. The evidence therefore supports
preflight/shared-circuit coupling as the primary cache-fill availability boundary; it does not
support changing cache payload or TTL.

## Boundary classification

1. **Primary:** concurrent, uncoordinated TCP preflight submissions to a one-thread executor.
2. **Primary amplifier:** a failed probe caches `False` for 250 ms and applies fallback to all
   Redis-backed request features.
3. **Secondary amplifier:** the authentication 200 ms wrapper calls `force_open()` for 30 seconds.
4. **Real but smaller:** Redis command timeouts under request pressure.
5. **Not supported as root cause:** cache serialization, cache TTL, or Redis pool exhaustion.

## One isolated proposed repair

Coalesce Redis TCP connectivity preflight into one in-flight probe per `CircuitBreaker` instance.
When the cached probe result is stale, elect one probe leader; concurrent callers await the same
shielded task instead of queuing independent work into the one-thread executor. Record at most one
fresh success/failure and one circuit failure for that probe generation. Preserve the existing:

- TCP socket probe and endpoint;
- 50 ms socket-connect timeout;
- 100 ms caller deadline;
- 250 ms result cache;
- circuit threshold and 30-second recovery;
- Redis commands, retries, fallbacks, and authentication behavior.

Do not combine this with another authentication single-flight experiment or change the 200 ms
authentication wrapper in the same slice.

Focused acceptance:

- concurrent stale-cache calls execute exactly one socket probe;
- all waiters receive the same probe result;
- follower cancellation does not cancel the shared probe;
- probe completion clears the in-flight entry;
- failed probe records at most one circuit failure;
- a subsequent generation can retry;
- endpoint separation remains per breaker instance;
- existing circuit/Redis tests stay green.

Frozen MODERATE acceptance remains unchanged. In particular, the repair must materially reduce
false TCP failures and shared fallbacks, restore at least 95% warm authentication cache hits, keep
API errors at or below 0.5%, meet existing latency/drain thresholds, and preserve all durable
correctness invariants. If it fails, revert it and do not mix authentication, quota, webhook,
feedback, or worker changes into the result.
