# Moderate Scale Harness Implementation v1

Status: implemented; isolated LOW baseline executed on 2026-08-14.

## Added assets

- `benchmarks/internal/scale-workload-v1.json`: frozen development-only workload, stages,
  resource caps, and LOW acceptance thresholds.
- `scripts/moderate_scale_k6.js`: mixed-traffic generator with an explicit dedicated-stack guard,
  LOW-only approval guard, request cap, API latency metrics, job polling, and versioned output.
- `benchmarks/internal/scale_harness.py`: preflight, database/job/outbox snapshots, correctness
  invariant audit, and run-scoped PostgreSQL/Qdrant/Redis cleanup.
- `tests/unit/test_scale_benchmark_harness.py`: safety, holdout exclusion, workload, API-contract,
  and deterministic metric tests.

These assets are deliberately not part of ordinary PR CI. Scale execution remains manual and is
not registered as an accepted benchmark suite until its first LOW baseline passes.

## Safe execution order

1. Provision a disposable PostgreSQL/Redis/Celery/Qdrant/API stack and a benchmark-only tenant,
   writer, API key, queues, and Qdrant namespace.
2. Set `MEMORYOS_SCALE_DEDICATED=1`, `BENCHMARK_API_KEY`, `SCALE_SOURCE_SERVICE`, and `RUN_ID` only
   in that stack. Never commit or print the key.
3. Run consolidated FAST and INTEGRATION correctness gates.
4. Run `python -m benchmarks.internal.scale_harness preflight --run-id <run-id>`.
5. Run `k6 run -e SCALE_STAGE=LOW scripts/moderate_scale_k6.js`.
6. Capture a snapshot, wait for outbox convergence, then run the invariant audit.
7. Run the post-load correctness gates and cleanup the run namespace.

## First LOW execution

The dedicated `memoryos-scale` Compose project and deterministic benchmark provider were used for
the first LOW run. Results and confirmed weaknesses are recorded in
`reports/moderate-scale-low-baseline-20260814.md`. No paid provider or holdout access occurred.
