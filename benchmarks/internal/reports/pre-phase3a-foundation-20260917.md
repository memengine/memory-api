# Pre-Phase 3A foundation verification

Date: 2026-09-17

## Scope

This checkpoint covers the tenant extraction path only. It does not change MCP
and it does not enable semantic confirmation. Universal extraction remains on
its existing governed legacy path with the modern extractor available only as
an opt-in shadow comparison.

## Enforced boundaries

- A direct user statement can continue through the existing evidence policy.
- `user_confirmed_assistant_proposal` is rejected by default with
  `semantic_confirmation_not_enabled`. Phase 3A must explicitly opt in after
  deterministic proposal resolution and calibration are connected.
- Tool output and fetched documents cannot become direct-user evidence.
- Model-cited turns must be fully present in the final bounded prompt.
- The primary system prompt plus user input is capped at 10,000 tokens.
- Proposal groups have stable IDs and ordinals, one serialized active group per
  user/conversation scope, deterministic expiry, and scoped resolution.
- Queue admission and release are single Redis atomic operations. Duplicate
  reservation/release cannot change another job's capacity.

## Capacity and observability

- Celery dispatch contains only the extraction job reference; workers load the
  encrypted persisted payload.
- Extraction workers use named plan queues, prefetch 1, and hard/soft task
  limits. Provider calls use shared concurrency leases and jittered retry
  delays.
- Each completed job persists queue wait and per-pass token/latency metadata.
  Provider failures persist classified error types, including rate limiting and
  timeouts.
- The internal queue-depth response exposes UTC-day admission attempts,
  queue-full count, and queue-full rate. ECS/CloudWatch remains the source for
  worker CPU saturation and queue-depth alarms.

## Verification evidence

- Focused evidence, extraction, queue, and schema tests: 47 passed.
- Full unit suite: 903 passed.
- PostgreSQL evidence/proposal, provider-lease, and Redis capacity integration
  gate: 3 passed.
- Real Redis burst: 100 simultaneous starter jobs admitted exactly 50 of the
  configured 50 slots, rejected 50, kept duplicate reservation idempotent, and
  returned depth to zero after owned releases.
- Migration downgrade and re-upgrade:
  `pre3a_proposal_order -> phase26b_evidence_ledger -> pre3a_proposal_order`
  completed successfully.
- Mypy: no issues in 230 backend source files.
- FAST deterministic benchmark: 8 of 8 suites passed with no product failures
  or harness errors. The first local run had only a Windows temp-directory ACL
  harness error; rerunning with a workspace pytest temp directory passed.

## Remaining release gates before semantic Phase 3A

1. Run the live development extraction evaluation on the final commit.
2. Run the blind holdout exactly once only after explicit approval. Deterministic
   FAST results do not establish real-model extraction accuracy.
3. Keep Phase 3A tenant-scoped until Universal shadow evidence demonstrates
   parity for authority, provenance, lifecycle, conflicts, and cost.
4. Do not enable generic confirmation from model self-reported confidence.
   Phase 3A still requires the calibrated resolver and ambiguity/pending path.
