# Redis First-Failure Hardening

## Summary
Redis outage handling is partially hardened.

What is fixed:
- Once a Redis outage is observed, the API degrades instead of failing broadly.
- Later requests return quickly with degraded behavior.
- `X-MemoryOS-Circuit-Status` reports `DEGRADED`.
- [`/health`](/d:/memoryos/memory-api/api/main.py) reports `status: degraded` and `redis: unavailable`.

What is still not fully fixed:
- The first authenticated tenant request after a hard Redis outage can still be slow on the local Docker setup.
- This affects the first request that discovers the outage on a given API instance.

## Current Behavior
Healthy baseline:
- First authenticated `GET /v1/tenant/usage` after API restart with Redis healthy: about `398ms`

Redis outage:
- First authenticated `GET /v1/tenant/usage` after `docker compose stop redis`: variable, observed around:
- `12.1s`
- `8.6s`
- `6.7s`
- `5.1s`
- `4.6s`

Steady degraded state after the breaker trips:
- Later authenticated tenant requests: about `240ms` to `375ms`
- `GET /health`: about `4ms` to `30ms`

This means the system is much safer than before, but the first-hit penalty still exists.

## Why This Matters
If left as-is:
- One unlucky request per API replica may be slow when Redis first fails.
- Customers may see a timeout-like experience right when the outage begins.
- In a horizontally scaled deployment, each fresh replica may pay this first-hit cost once.

What is no longer happening:
- Redis outage no longer causes sustained platform-wide `500` behavior.
- The system now enters degraded mode and keeps serving.

## Improvements Already Implemented
These changes are already in the codebase:

1. Redis circuit breaker uses local state for its own breaker state.
2. Redis breaker async calls use a fast timeout path.
3. API-key auth marks Redis unavailable immediately when Redis cache lookup/write times out.
4. Redis-dependent code paths degrade cleanly instead of propagating raw failures.
5. Health and circuit headers expose degraded state correctly.

Important note:
- A background Redis monitor was tested and then removed.
- It created repeated DNS-failure noise in this Docker environment and was not kept.

## Likely Root Cause
The remaining delay appears to be environment-level failure latency on the first Redis connection failure path, likely involving Docker networking and name resolution, not the later circuit-breaker fallback logic.

Evidence:
- Healthy first tenant request is fast.
- Only the first request after Redis is stopped is slow.
- Subsequent requests are fast and degraded.

## Recommended Future Fix Path
Treat this as infrastructure-aware hardening, not just application retry logic.

Recommended order:
1. Reproduce on the production-like environment, not only local Docker.
2. Inspect Redis host resolution and TCP failure timing at the container/network level.
3. Consider service-discovery or connectivity preflight outside the normal request path.
4. Verify behavior per API replica in a multi-replica deployment.

Potential approaches:
- Redis connectivity preflight during request admission using a lower-level socket strategy tied to the resolved endpoint
- container/network-level health-backed Redis discovery
- sidecar/proxy or connection manager that fails faster than the current client path
- startup warming or connectivity priming for authenticated request paths

## Acceptance Criteria For The Future Fix
This item can be considered closed when all of these are true:

- First authenticated tenant request after Redis outage degrades in under `500ms`
- Subsequent requests remain degraded and fast
- No sustained `500` responses due to Redis outage
- No background log noise or runaway failed futures
- Works consistently across multiple API replicas

## Suggested Verification Commands
Re-run these when returning to this task:

```powershell
docker compose up --build -d api redis
docker compose stop redis
```

Then time:
- `GET /v1/tenant/usage` with a valid API key
- `GET /health`
- `GET /v1/internal/circuit-health`

Expected future target:
- first tenant request under `500ms`
- `X-MemoryOS-Circuit-Status: DEGRADED`
- `/health` shows `redis: unavailable`

