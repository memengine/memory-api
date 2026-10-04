# V2 preference development diagnostic preparation

Offline preparation only. This command has **no execute option**, does not load
credentials, creates no provider client, writes no artifacts, reads no annotations
or private holdouts, and cannot enable production. Preparation is not spending
approval. The frozen v1 and v2 contracts remain unchanged.

```powershell
.venv\Scripts\python.exe -X utf8 -m benchmarks.internal.preference_binding_development_v2 --dry-run
```

## Fixed question and source coverage

Can an interpretation-only output produce useful, attributed property/scope/
stance/value proposals while the backend owns target binding? This evaluates the
whole v2 design, not a single-factor causal experiment. Prompt wording, source
projection and output schema change together. No v1 control is rerun and no
consumed authorization or historical journal is reused.

The 24 familiar synthetic development cases are eight related groups with one
English, Hindi and Hinglish case each. They are neither independent samples nor
blind holdout evidence. The fixed IDs are:

| Scenario | English | Hindi | Hinglish |
| --- | --- | --- | --- |
| Adopted default | d0001 | d0002 | d0003 |
| Tentative interest | d0031 | d0032 | d0033 |
| Undecided, no alternative | d0091 | d0092 | d0093 |
| Explicit replacement | d0121 | d0122 | d0123 |
| Scoped exception | d0151 | d0152 | d0153 |
| Rejected alternative | d0181 | d0182 | d0183 |
| Unrelated question/fact | d0241 | d0242 | d0243 |
| Ambiguous reference | d0271 | d0272 | d0273 |

Dispatch order starts with d0001, d0121 and d0151, then remaining cases in the
table's group order. All sources retain `client_assertion`; there is no synthetic
promotion to direct user authority. The existing fixed public dataset loader
checks the development seal; only these source cases become requests. Author
coverage tags are local manifest data, not model input, ground truth or a score.
This subset does not cover every property domain or live adversarial scenario.

Complete synthetic fixture coverage enumerates every offered property/scope,
including absence. It is not inferred from public/vector retrieval, and is not
SQL completeness or concurrency proof. The shown fixture is also used as current
for this diagnostic; production fresh-state and transactional checks remain pending.

## Provider shape and seals

One fixed v2 instruction asset contains no per-case expected answer, worked
example or phrase recognizer. Target selection instructions are replaced with
backend-owned exact-identity binding. No target references, IDs, versions,
authority, coverage, confidence or activation fields enter the model schema.
Existing values and choice states remain semantic context.

The narrow adapter changes only the closed kind-tagged union from `oneOf` to
`anyOf`, removes its discriminator, and converts the two fixed kind constants to
singleton enums. Other constraints are preserved; unsupported schema changes
block preparation. Exact UTF-8 wire bytes preserve `kind` first, followed by
property, scope, stance, evidence and value. Canonical hashes alone cannot seal
schema ordering. Provider acceptance still needs the separately approved first
paid request; mocks do not establish it.

The plan digest seals source code, both contracts and their direct source-policy/
applicability dependencies, instructions,
dataset/candidate protocol, every full validation wrapper (including coverage),
canonical request meaning, exact wire bytes, validator selection, fixed stop
policy and cap. Any change requires a new digest and separate run approval.

The existing shared executor defaults to v1 validation. V2 is an explicit,
digest-bound selection, not an injectable callback or automatic format detector.
It requires complete v2 wrappers, source seals and exact wire/context hashes,
then delegates to the frozen v2 binder and v1 policy. It does not repair old v1
responses, remove forbidden fields, coerce stance or increase authority. V2 plans
and wrapper contexts are snapshotted before transport can mutate caller-owned
objects. This is offline fixture isolation, not a production race guarantee.

## Proposed execution bounds, not current authorization

The unchanged baseline is `gpt-4.1-mini-2025-04-14`, Chat Completions, temperature
0, 1,024 completion tokens, explicit default tier, one in-flight call and one
attempt per case. Keeping the model fixed avoids a simultaneous model change.
Official [model documentation](https://developers.openai.com/api/docs/models/gpt-4.1-mini)
lists the snapshot, Structured Outputs support and standard text rates of $0.40
input / $1.60 output per million tokens (checked October 4, 2026). Account access
and this specific adapted schema's acceptance have not been verified live.
The [Structured Outputs guide](https://developers.openai.com/api/docs/guides/structured-outputs)
documents schema/output ordering and warns that structured results can still
contain mistakes; valid JSON is not semantic correctness.

The proposed cap is $0.50 for this run's token charges, not unrelated account
activity or an invoice guarantee. Reuse the existing $0.43 per-attempt reservation
from the model context ceiling; do not replace it with an estimated byte count.
The printed planning cost uses request byte counts plus framing padding, not exact
tokenization or a guaranteed billed-token upper bound. Reconcile uncached charges
from valid usage; retain uncertain reservations and stop.

Future execution must retain the shared durable one-shot lock/journal, exact-body
dispatch, zero retries, no fallback, no redirects, bounded response envelope,
30-second HTTP phase timeout, standard-tier usage checks and stop on any transport,
refusal, truncation, output or frozen-validator failure. A stopped or interrupted
authorization is consumed, never resumed by deleting its lock.

Additionally, stop after the first three smoke cases if **all have zero admitted
assessments**. This is a structural futility condition, not proof of model error
or a semantic pass threshold. One accepted assessment merely permits the bounded
remaining diagnostic; it does not establish that assessment was correct. There
is no prompt adjustment, repair, model switch or repeat within the run.

No live invocation is provided by the preparation CLI. Separate approval must name
the exact new digest, pinned model, $0.50 cap, synthetic-source sharing and
`artifacts/preference-binding-development-v2/run-<digest>/`. The preparation-only
approval does not authorize calling the shared executor with a real provider.

## Honest reporting and next decision

Seal predictions before inspection. Report attempts versus unexecuted cases,
raw interpretations versus bound assessments, abstentions, exact rejection
reasons, quoted evidence, recorded latency and reconciled cost separately. A
valid abstention is not successful preference interpretation. Inspect stance,
negation, scope, evidence and omissions, including tentative/rejected cases; do
not reinterpret interest as adoption or count all validator passes as accuracy.

No AI draft labels, human worksheets or holdouts are loaded. There is no automated
semantic score, calibrated confidence, precision/recall, production p99, release
pass or permission to write memory. Without independent human labels, a report
may identify concrete development observations, not claim general accuracy.
After a frozen rejection, inspect the sealed failure rather than weaken validation
or initiate another paid trial. If the design produces useful observations, the
next step is separately reviewed broader independent evaluation and a complete
SQL/write/read adapter design—not production enablement.
