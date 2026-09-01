# LoCoMo official dataset validation v1

Date: 2026-08-29

## Pinned source

- Repository: `https://github.com/snap-research/locomo`
- Upstream `main` revision: `3eb6f2c585f5e1699204e3c3bdf7adc5c28cb376`
- Dataset: `data/locomo10.json`
- Dataset SHA-256: `79fa87e90f04081343b8c8debecb80a9a6842b76a7aa537dc9fdf651ea698ff4`
- Local path: `benchmarks/public/data/locomo10.json` (Git-ignored)

## Validated accounting

- Conversations: 10
- Sessions: 272
- Turns: 5,882
- QA records: 1,986
- Category 1: 282
- Category 2: 321
- Category 3: 96
- Category 4: 841
- Category 5: 446

The live upstream schema stores `img_url` as a list even for a single image. The offline
contract was corrected to preserve that representation. This was benchmark harness drift, not a
MemoryOS product failure.

## Upstream annotation diagnostics

Three evidence references do not resolve to a dialog ID in their source conversation:

| Question ID | Missing evidence reference | Classification |
| --- | --- | --- |
| `conv-42:qa-58` | `D10:19` | upstream annotation drift |
| `conv-47:qa-38` | `D4:36` | upstream annotation drift |
| `conv-50:qa-69` | `D30:05` | upstream annotation drift / possible zero-padding mismatch |

No label was repaired or normalized. These cases must remain visible in full-dataset reporting so
retrieval failures are not incorrectly attributed to MemoryOS. None occurs in the frozen pilot.

## Frozen pilot

Manifest: `benchmarks/public/locomo/samples/pilot-v1.json`

- Conversations: `conv-43`, `conv-48`
- Questions: 25
- Distribution: five questions from each category
- Selection inputs: sample ID, QA index, and category only
- Answers and evidence excluded from selection
- Classification: public benchmark pilot, not an official LoCoMo score

The manifest is intended to validate wiring and localize architecture failures. It must not be
used for product or prompt tuning, and its result must not be marketed as the full LoCoMo score.

## Execution boundary

No MemoryOS API, extraction provider, embedding provider, answer model, or judge was called during
this step. No internal holdout was accessed and no production behavior changed.
