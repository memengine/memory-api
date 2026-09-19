# Phase 3A calibration and holdout decision

Date: 2026-09-18

## Decision

Phase 3A remains disabled. The development calibration passed its frozen gate,
but the single approved blind holdout did not. Do not enable
`PHASE3A_CONFIRMATION_ENABLED` and do not tune production behavior against the
individual holdout cases.

## Development calibration

Dataset: 100 labeled cases, with at least 30 cases per language and 20 cases per
reference type. Languages were English, Hinglish, and Hindi. Reference types
were single vague acceptance, single indirect acceptance, explicit ordinal
selection, ambiguous multi-proposal acceptance, and rejection/question.

Final artifact:
`artifacts/internal-benchmarks/phase3a/phase3a-development-20260918-v3.json`

- completed: 100/100; provider errors: 0
- accepted precision: 1.00
- accepted recall: 0.95
- ambiguous pending recall: 0.90
- rejection recall: 1.00
- evidence integrity: 1.00
- mean latency: 4.087 seconds; p99 latency: 11.308 seconds
- estimated provider cost: USD 0.05734 for 100 turns
- release gate: passed

Model self-reported confidence remained diagnostic only and never granted
proposal authority.

## Approved holdout

A replacement holdout was generated with `gpt-4.1-mini`, while evaluation used
the configured production extraction model `gpt-4o-mini`. The preparation
script validated only schema, slice counts, uniqueness, and zero exact overlap
with development; case contents were not printed during evaluation.

- cases: 50
- language slices: English 15, Hinglish 15, Hindi 20
- dataset SHA-256:
  `5dfc761d0e5b3164d91ec9d08727f62a78d7e93f2a64db5b78c9f211c30e88bc`
- execution count: one
- artifact:
  `artifacts/internal-benchmarks/phase3a/phase3a-holdout-v1.json`

Aggregate result:

- completed: 50/50; provider errors: 0
- accepted precision: 1.00
- accepted recall: 0.7333
- ambiguous pending recall: 0.30
- rejection recall: 1.00
- evidence integrity: 1.00
- mean latency: 3.177 seconds; p99 latency: 5.225 seconds
- estimated provider cost: USD 0.02794
- release gate: failed

This generated holdout is useful as an independent model-generated challenge
set, but it is not a substitute for a human-labeled design-partner set. Because
it failed, no production activation decision depends on that distinction.

## Production changes exercised

- Proposal-confirmation evidence now derives the later eligible user turn from
  the server-trusted transcript instead of trusting the model's user-turn index.
- An explicit active `proposal_turn` is evaluated through the stricter proposal
  policy even if the model labels the dependent reply as a direct statement.
- Tool and document sources remain ineligible as user confirmation evidence.
- Explicit refusals and questions are denied before proposal binding.
- Deterministic ordinal parsing covers the evaluated English, Hinglish, and
  Hindi forms.

## Verification

- Focused Phase 3A tests: 70 passed.
- Full unit suite: 937 passed, 22 warnings.
- Full backend mypy: no issues in 230 source files.
- PostgreSQL evidence-ledger integration: 1 passed against the local Docker
  PostgreSQL database at `localhost:5432`; `.env` was explicitly loaded for the
  test process.
- `git diff --check`: no whitespace errors; only Windows line-ending warnings.

## Next gate

Do not reuse or inspect the failed holdout for tuning. The next evidence should
come from manually labeled internal dogfood or design-partner confirmation
turns, followed by a new independently prepared holdout. Until then, keep the
feature flag off and leave public documentation unchanged.
