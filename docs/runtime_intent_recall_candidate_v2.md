# Runtime recall/operation candidate v2: evaluation only

Status: implemented internal candidate, NOT promoted to the queue worker and
NOT a production repair/acceptance pass. No paid provider evaluation performed
for this implementation slice. The latency NO-GO remains unchanged.

## What changes

The existing `classify_intents` function accepts an internal keyword-only
`contract_version=2`. Its default is still `1`, and
`RuntimePreferenceWorker.process()` still calls it without an override.
There is no public request selector, job/metadata selector, live flag, SDK
method, migration or second classification pipeline in this slice.

V2 uses the same transcript/catalog/snapshot construction, provider adapter,
one primary call, output bounds, `PreferenceIntentReply` parser and evidence
binding. It changes only the instructions and provider-facing generation schema:

- The task is preparation of user-expressed memory operations, not answering
  recall questions. Merely asking what is stored is not reaffirmation.
- Existing values identify targets but never become user evidence.
- Mixed turns retain separately expressed updates and omit recall-only clauses.
- Requests to set a lasting preference can be question-shaped; punctuation or
  opening words are not the semantic definition of read versus change.

Value constraints sent to the provider:

| Action | Value | Property/scope |
| --- | --- | --- |
| `assert`, `set_current` | Required string | Both required strings |
| `withhold`, `reaffirm` | Required `null` | Both required strings |
| `tentative`, `contextual` | Required string or `null` | Both required strings |
| `abstain` | Required `null` | Both strings, or both `null` |

The five closed nested `anyOf` branches derive common fields/bounds from the
canonical model. The root remains a closed object; fields remain required.
No independent copy of the persisted parser or writer was introduced.
The structure follows the supported nested-alternative approach in
[official OpenAI documentation](https://developers.openai.com/api/docs/guides/structured-outputs).
Actual provider acceptance/performance has not been measured for v2.

## What deliberately does not change

Persisted decision schemas/hashes and previously captured/applied receipts are
unchanged. The original v1 prompt is byte-identical to its pre-candidate value:
SHA-256 `2beb2e2a2fe8ac0ab40c8cc8535a93aec2952b764ce98c1aa5e7ff6207b9371c`.

Invalid JSON, unsupported evidence, unknown identity/scope, forged authority and
source-role violations still fail the existing backend checks. An invalid reply
does not become empty/no-change, get repaired, trigger another classifier call,
release a guard, or receive permission to mutate memory.
No classifier label or empty array is an independently calibrated meaning proof.

The initial classifier still permits at most 128 KiB prompt and 64 KiB response
bytes, uses temperature 0 and an 8,192 completion-token ceiling. V2's serialized
schema is larger than v1 (4,214 versus 1,893 bytes in this local environment).
Neither a smaller latency nor a lower cost is claimed.

`jsonschema>=4.20,<5` was added to development dependencies only, for independent
JSON Schema contract validation. It is not imported by runtime code and adds no
runtime provider call or SDK dependency. A power interruption corrupted its
initial local installation; the affected dependencies were reinstalled at the
same installed versions before testing.

## Known semantic gate limit: do not hide it behind passing tests

The correct candidate finding for a persistent request such as:

> Could you make Python my default for every programming example?

is structurally representable as `set_current` with exact user evidence. The
unchanged backend's existing question-only evidence heuristic nevertheless
rejects that form. A controlled contract test explicitly reproduces the rejection.
That test passing verifies the known limit; it does NOT mean the user request
works. No phrase-specific exception or evidence-gate weakening was implemented.

Therefore a prompt/schema improvement alone cannot establish complete P1-P9
pilot readiness. Do not spend credits rerunning the whole failed batch or
promote v2 merely because its structural tests pass. Any later binding-policy
repair requires its own narrowly approved scope and evidence; it cannot be
silently folded into this candidate or bypass backend governance.

## Frozen prospective development inventory

File: `benchmarks/internal/datasets/governed_memory/development/recall_operations_v2_v1.json`.
Version: `recall-operations-v2-development-v1`. Exactly 21 cases: seven each in
English, Hindi and Hinglish. Seven families, three cases each: recall,
reaffirmation, correction, uncertainty, mixed recall/update/fact, one-off request,
and question-shaped persistent update.

UTF-8 text SHA-256 after checkout newline normalization to LF:
`39914b6bb61805bd89e59f6bac8b87df2d7b28c7c99141ab8ee6105f10c77f1f`.
This is a development inventory with AI-authored diagnostic expectations, not
independent human gold labels, calibration data or an untouched holdout.
It does not replace or modify previous consumed runs or blind holdouts.

A later evaluation must construct fresh synthetic owned snapshots with the
declared initial C++/global/authority-20 state and actual `role=user`,
`source_kind=client_assertion` turns. No customer data or live records may be
sent. Preserve complete raw outputs before parsing, request/provider/prompt/schema
identifiers, usage/timing and exact schema/binding errors. Positive findings must
match the offered language identity and actual quote; expected action alone is
not enough. The mixed family must also preserve its stated exam-date fact.

For recall, empty arrays are expected. For one-off requests, an empty reply or
an eligible `contextual` finding is acceptable, but neither may change the
current default. Explicit reaffirmation, correction and uncertainty must not be
accepted as empty merely to remove a failing result. The question-shaped update
family stays in the inventory and must not be skipped or relabeled as recall.

No paid evaluation is authorized by this document. Before one, freeze request
bytes and expected diagnostic interpretations, verify the complete gate is
representable, and approve the run and spend ceiling. Use the existing classifier
opt-in; do not build another generic runner or add hidden retries/fallbacks.

## Implementation verification (2026-10-06)

- 111 focused v1/v2 schema, parser, binding and footprint contract checks passed.
- 2,452 tracked unit tests plus the new candidate test module passed in total.
- 124 isolated PostgreSQL regression checks passed: existing runtime worker,
  intent/fact transactions, pending visibility and authenticated turn preparation.
  These database tests retain the worker's v1 selection; they are compatibility
  evidence, not live v2 semantic evidence.
- FAST gate: 9/9 passed; reported provider cost $0.
- Scoped type checking of the two changed service modules passed; fatal-error /
  undefined-name lint passed. No claim that unrelated backend typing is clean.
- Original v1 prompt equality and default worker call selection were verified.
- No paid provider calls, holdout runs, package publication, push, deployment,
  assistant/MCP changes or production activation occurred.

These passing checks do not remove the known question-shaped-update rejection
or measure candidate semantic accuracy, model availability, calibration, latency
or production load capacity. The frozen pilot checklist remains the release gate.
