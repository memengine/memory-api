# Internal benchmark consolidation v1

## Inventory

The manifest registers 17 executable or sealed suites: 8 FAST, 5 INTEGRATION, 3 PROVIDER,
and 1 manually sealed holdout suite. It also indexes 11 reviewed reference baselines that are
historical/shadow/offline records rather than independently executable gates.

Existing assets retained rather than replaced:

- Frozen development datasets for extraction, importance generalization, conflicts, retrieval,
  historical retrieval, temporal behavior, lifecycle/provenance, multi-agent coordination,
  agent deletion, integration reliability, governance integrity, and fault injection.
- The extraction holdout remains at its existing path but is excluded from every routine tier.
- Existing suite runners, baseline JSON files, reports, and machine-readable artifacts remain
  authoritative inputs. The orchestrator composes them; it does not reimplement their logic.
- Production-active accepted components include evidence attribution, conflict/claim repairs,
  concurrency constraints, temporal validity/transitions, retained supersession vectors,
  retrieval score filtering, event idempotency, revocation/deletion governance, and lifecycle
  behavior. Deterministic importance remains development shadow-only. The Celery crash barrier
  is benchmark-only and unavailable in production.

## Tiers

- FAST / PR: eight deterministic unit/contract suites; no provider or external paid calls.
- INTEGRATION: fault injection, integration reliability, governance integrity, lifecycle
  activation, and temporal memory against configured PostgreSQL/Redis/Celery/Qdrant services.
- PROVIDER: extraction, live-vector retrieval, and historical retrieval. Requires an explicit
  approval flag and configured provider credentials; never runs on ordinary PRs.
- HOLDOUT: no routine runner. It requires a separate tier, explicit CLI flag, and exact approval
  environment token, then still stops for a separately reviewed manual command.

## Current validation

- FAST: 8/8 passed; 28.87 seconds; $0 provider cost.
- INTEGRATION: all five suites passed after repairing the stale dead-letter test fake. The three
  previously passing suites consumed 625.85 seconds; the two repaired/rerun suites consumed
  313.82 seconds. Effective consolidated runtime: 939.67 seconds; $0 provider cost.
- PROVIDER: deliberately not executed in this phase because paid calls require separate explicit
  approval. The unapproved command fails closed.
- Holdout: not read. Both the loader and orchestrator fail closed without dual authorization.

The integration orchestrator resolves absent host `DATABASE_URL` by executing runners inside the
configured local Compose API container. This made all PostgreSQL scenarios evaluable. The only
remaining drift found was `FakeExecuteResult.scalar_one_or_none`; the fake was repaired without
changing production code or thresholds.

## Gate behavior

Each suite is evaluated using its manifest acceptance rules. Product failures and harness/config
failures are reported separately. Missing result metrics are harness errors. Numeric values shared
with a JSON accepted baseline are emitted as deltas. Any product failure or harness error fails the
tier; a configured missing provider skips that provider suite with a reason after explicit approval.

Aggregate output is versioned under `artifacts/internal-benchmarks/aggregate/<run-id>/` as both
`aggregate.json` and `aggregate.md`.
