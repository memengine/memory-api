# Phase 3A confirmation shadow mode

Date: 2026-09-19

## Decision

Phase 3A live confirmation remains disabled. A separate, disabled-by-default
shadow flag now permits production observation without allowing any
proposal-derived candidate to reach pending-candidate persistence, conflict
resolution, or memory storage.

## Runtime boundary

- The normal extraction call runs first and is unchanged.
- Shadow mode performs a database pre-filter for an active, registered proposal.
- No second model call occurs when that pre-filter is empty.
- Eligible shadow evaluation uses copied messages and an isolated extractor.
- The evaluator returns only bounded aggregate diagnostics. Candidate content,
  evidence text, and model output are discarded.
- Shadow failure is fail-open and cannot fail the normal extraction job.
- If the live flag is enabled, shadow mode is automatically suppressed.

## Configuration

- `PHASE3A_CONFIRMATION_ENABLED=false`
- `PHASE3A_CONFIRMATION_SHADOW_ENABLED=false`

The shadow flag must remain false until the corresponding code is deployed.
Enable it independently for an observation window; do not enable the live flag.

## Verification

- Focused shadow and extraction tests: 18 passed.
- Full unit suite: 943 passed.
- Backend type check: 230 source files passed.
- Terraform formatting and validation: passed.
- Local PostgreSQL evidence-ledger integration: passed.
- FAST benchmark gate: 8 of 8 suites passed, with zero product failures and
  zero harness errors. The first run encountered a Windows temporary-directory
  ACL harness error; rerunning with a workspace-local temporary directory passed.

## Observation exit criteria

Before considering the live flag, collect aggregate counts and latency/cost for
eligible turns, manually label a blinded sample, and evaluate accepted recall,
pending recall, rejection precision, binding accuracy, and evidence integrity.
The consumed holdout must not be reused for tuning or release approval.
