# Phase 3A tenant confirmation implementation

Date: 2026-09-18

## Scope

This change implements the tenant extraction path for a user confirming an
explicit assistant memory proposal. It does not change MCP or Universal
extraction. The production flag remains disabled by default.

## Authority boundary

- The existing extraction call identifies a confirmation and proposal turn; no
  second model call is added.
- The backend accepts that relation only when the proposal belongs to the
  latest active group for the same tenant, proxy user, and conversation.
- The proposal turn ID and content hash must match the immutable evidence
  ledger and the complete turn must have been visible to the extractor.
- A vague confirmation is accepted only for one active proposal. Multiple
  proposals require an explicit ordinal; ambiguity becomes a non-promotable
  pending candidate.
- Tool output, fetched documents, inactive proposals, hash mismatches, expired
  proposals, and stale concurrent resolutions cannot grant authority.
- Accepted proposals are claimed in the same database transaction as memory
  storage. A failed storage transaction rolls the proposal claim back.

## Rollout gate

`PHASE3A_CONFIRMATION_ENABLED` defaults to `false` in application settings,
local examples, and Terraform. Do not enable it until the repeatable
development calibration and the separately approved blind holdout pass. The
model's self-reported memory confidence is not treated as a calibrated
confirmation probability.

## Verification

- Full unit suite: 918 passed.
- Focused PostgreSQL evidence/proposal integration: 1 passed, including replay
  of an already registered proposal without replacing its active group.
- Full backend mypy: no issues in 230 source files.
- FAST deterministic benchmark: 8 of 8 passed. The first run had only the known
  Windows temporary-directory ACL harness error; the workspace-temp rerun
  passed.
- Live confirmation smoke: negative and question cases were rejected; natural
  single-proposal, explicit ordinal, and Hinglish confirmations were accepted;
  vague multi-proposal confirmation was held pending. One indirect English
  wording remained model-variable across repeated calls, so the flag remains
  off pending a larger calibration set.
- General live development extraction: 45 completed cases and four OpenAI
  connection failures. Completed-case precision was 0.978, recall 0.846, F1
  0.907, with no forbidden leaks. The run is not release eligible because of
  the provider failures.

The blind holdout was not opened or run.
