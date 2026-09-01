# Authentication-cache availability diagnosis

Date: 2026-08-26  
Source run: `cache-generation-conflict-moderate-rerun-20260826`  
Status: **read-only diagnosis complete; no production behavior changed**

## Finding

The 4.62% reported authentication-cache hit rate does not prove that positive entries expire or
disappear. The auth metric labels every `None` result as `miss`, including:

- a genuinely absent Redis key;
- a call rejected by an open circuit;
- a fresh TCP-preflight failure;
- a cached negative preflight result;
- a Redis exception converted to the fallback value.

Therefore 3,084 reported misses are mostly an availability/fallback signal, not a key-existence
measurement. Cache payload validation, the five-minute TTL, generation invalidation and explicit
auth-key deletion are not implicated by the captured evidence.

## Evidence

- Redis circuit events: 8,033; transitions: 768; OPEN transitions: 234.
- OPEN sources: TCP preflight 174, auth outer deadline 40, circuit execution 20.
- All failure transitions: TCP preflight 590, auth outer deadline 87, circuit execution 91.
- Circuit gates: 3,359 — cache role 2,713 and auth role 646.
- Actual Redis command errors excluding `CLIENT` negotiation telemetry noise: nine `GET` timeouts.
- Connection and pool-acquisition errors: ten each; no max-connection signature.
- Request-path `SCAN`: zero.
- Redis post-run resource snapshot: about 10 MiB memory and below 1% CPU, so the artifact does not
  support Redis server resource exhaustion.

The preflight is local work: a single-thread executor runs a 50 ms socket connection under a
100 ms caller deadline, with a 250 ms cached result. Successful sampled probes completed at
p50/p95/p99 39.7/81.7/86.0 ms. Failed probes were recorded at 384/884/1,563 ms, well beyond the
configured deadline. That shape indicates event-loop/executor descheduling rather than a clean
50 ms network refusal. Each cached negative result then prevents multiple real Redis commands.

Auth and general cache clients use separate pools but the same process-local Redis breaker. Cache
traffic produced 675/768 failure transitions, so cache-path preflight failures suppress auth GET
and SET operations. Open-gate fallback returns `None`; auth subsequently records `miss` and repeats
PostgreSQL plus bcrypt. Positive writes are also silently skipped while the breaker is open.

The first OPEN was initiated by five auth 200 ms outer-deadline failures. Immediately afterward,
cache preflight failures became the sustained source: 74.4% of OPEN transitions and 76.8% of all
failure transitions. This is a feedback loop, not a TTL-calibration problem.

## Existing evidence considered

The earlier benchmark-only preflight bypass passed LOW with a 99.71% auth-cache hit rate and no
Redis/pool/connection timeouts. An older MODERATE command-driven run still failed overall capacity,
so disabling preflight is not assumed to solve every MODERATE bottleneck. Later failure-ownership,
conflict and transaction repairs changed the current baseline, making a current one-change rerun
necessary before any production decision.

## One isolated experiment proposed

Use the already-existing guarded benchmark setting `BENCHMARK_REDIS_TCP_PREFLIGHT=disabled` under
the exact frozen MODERATE workload and current accepted repairs. Keep generation invalidation v2
enabled. Do not modify production defaults.

Keep unchanged: Redis connect/command/circuit deadlines, retry/fallback logic, pools, circuit
threshold/recovery, auth payload/TTL, bcrypt/database behavior, workload, extraction, retrieval,
claims and correctness thresholds.

Acceptance:

- TCP-preflight events: zero by construction;
- actual Redis command, connection and pool failures do not increase versus this run;
- auth-cache hit rate >=95% after warm-up;
- DB/bcrypt fallback <=1% of authenticated requests;
- API and HTTP failure rates <=0.5%;
- frozen add/retrieval/job latency thresholds pass;
- zero unfinished jobs and complete outbox convergence;
- all durable correctness and post-load FAST/INTEGRATION gates pass;
- zero provider cost and no holdout access.

If it fails, keep both preflight bypass and generation invalidation benchmark-only and move the
capacity investigation to API/worker transaction occupancy rather than further Redis tuning.
