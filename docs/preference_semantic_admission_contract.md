# Offline preference semantic-admission contract v1

## Status and scope

This is an offline interpretation contract, not production admission. It has no
provider client, write adapter, runtime registration or dataset. No holdout is
opened or approved by this protocol. Existing extraction, retrieval and feature
flags are unchanged; explicit application-declared preferences remain opt-in.
The retained fixed model comparison did not qualify an arm. Schema tests do not
reverse that finding or demonstrate natural-language accuracy.

The contract is `benchmarks/internal/preference_semantic_contract.py`. The frozen
development protocol is
`benchmarks/internal/protocols/preference-semantic-admission-v1.json`.
Its sibling `.freeze.json` binds the UTF-8 text of the contract, protocol and this
document, normalized to LF line endings so Git's Windows checkout conversion
does not invalidate the freeze. Changes require a newly reviewed, versioned freeze before execution;
the freeze is not permission to call a provider or enable admission.

## Trusted input and interpretation output

The future backend adapter must supply an authenticated principal, an offered
property catalog, known applicability scopes, owned target snapshots and full
source turns. Properties describe reusable meanings, not particular programming
languages or magic confirmation phrases. Catalog registration and its permissions
are not implemented in this slice. Context scope keys are case-sensitive. Unknown
properties, scopes or ambiguous references must abstain, not silently choose
global applicability.

Offline input limits are 16 properties, 32 scopes, 32 targets, 64 turns, 8,000
total source characters and 32,000 serialized context characters. Duplicate
identities, foreign targets and unresolved offered scopes are rejected. Oversized
input is rejected, not truncated around qualifying words. These are work bounds,
not proof of latency under load.

`model_input` removes owner IDs, memory IDs, versions, authority and persistence
controls. It exposes catalog descriptions, scope and target references, existing
values/choice state, and complete role-labelled turns. Its only cheap pre-filter
skips blocked principals and inputs with no eligible user turn. A question or
mixed question/update is not dropped by an intent regex. A more selective filter
would need its own recall evaluation; no embedding/model pre-filter is assumed.

`response_schema` constrains references to the offered catalog. It is standard
JSON Schema, not tested with any provider's strict-schema dialect. There is no
provider prompt, response-format integration or runnable provider CLI here.

Each of at most 16 findings is either an assessment or an explicit abstention.
An assessment supplies property/scope/target references, an exact user quote, a
stance and an optional separately quoted value. Stances are:

- `asserted`: a proposed current value with value evidence.
- `replacement`: a proposed replacement, with value evidence and existing target.
- `current_choice_unresolved`: the existing choice is undecided, with existing
  target; an alternative value is not required.
- `tentative_interest`: interest alone, not a request to suspend a default.
- `reaffirmed`: existing choice reaffirmed, with existing target.
- `rejected_alternative`, `historical`, `unclear`: interpretation only.

There are no model-supplied authority levels, confidence scores, memory IDs or
activation commands. Nullable value evidence avoids copying the old value or
inventing an alternative when the user only expresses uncertainty. A quoted
whole sentence is not automatically a usable value: correct value boundaries
remain an independently evaluated semantic dimension.

The existing public SDK declaration requires a value inside its quote. It cannot
faithfully represent a value-free unresolved finding. Do not coerce this output
into that declaration by copying an old value, fabricating a quote or modifying
the customer's assistant. A future internal adapter requires separate approval
and must reuse the backend preference policy, not create a second memory store.

## Deterministic validation is not semantic proof

`validate_reply` compares shown and current trusted offline snapshots. It checks
principal/permission changes, catalog changes, source changes, owned target
identity, currentness, exact version/authority/choice-state equality and duplicate
assessment identities. Referenced targets cannot be invented or rebound to a
different property or scope. Omitting a target cannot bypass an existing winner.
Replacement, unresolved-choice and reaffirmation proposals cannot override a
target above the application-assertion authority cap of 20.

Evidence uses the existing eligible-user-turn policy and exact unique quote
validator. Tool, document, assistant and system turns are not eligible evidence;
content merely styled as a user confirmation does not change its recorded role.
Non-user context stays role-labelled and untrusted. A submitted `role=user` is
not cryptographic attestation. In a future adapter these application-originated
claims stay client-asserted at cap 20; interpreting them cannot raise authority.
Separate value evidence must be in the same eligible quote and turn.

Validation is all-or-nothing. Failure returns no partial assessment. Success
means only `attributed_proposals_only`, always with
`automatic_activation_allowed=false`. The pure comparisons here are not live
SQL reads, row locks or protection against a production race.

Important counterexample: “I do not want Python as my default” contains the exact
value “Python”. A reply incorrectly labelled `replacement` can pass structural
attribution checks. These checks cannot prove adoption or scope entailment; the
semantic evaluation must score that reply as wrong. Unit tests deliberately
preserve this counterexample. They do not use regex to pretend to understand it.

## Future write and read boundaries must ship together

Before automatic interpretation can affect stored context, its adapter needs
fresh SQL ownership/permission/authority checks and transactional version guards
through the existing governed policy. One lane must own each property; ordinary
extraction and a new classifier must not both mutate the same preference. Shadow
assessment must have no write effects.

Scope-aware reads must be integrated in the same release: trusted application
context chooses known applicability and up to 16 requested property keys; reads
do not add a model call. Existing unscoped reads exclude typed preferences. Do
not turn on automatic writes while customers still read only the legacy path.
Any inferred request scope/property routing needs its own evaluated contract;
the assistant must not be hardcoded to make backend results appear correct.

Interpretation should remain off the answer's critical path. A future adapter
still needs bounded queues, provider admission, timeouts, retry/idempotency and
measured load/cost behaviour. No p99, provider token cost or immediate
read-after-write guarantee is claimed here. Failure leaves existing governed
state unchanged; that does not mean user uncertainty was successfully understood.

This slice does not repair legacy fact/exam-date corrections, migrate old
preferences, implement proposal windows, enable universal memories or alter
customer-facing UI. Those cannot be claimed fixed by this preference contract.

## Frozen development evaluation, not repeated tuning

No dataset is assembled yet. Planned minimum development coverage is 360 cases:
10 cases for each of 12 families in each of English, Hindi and Hinglish. Six
reference slices additionally need at least 10 cases per language and may overlap
family coverage. Families include uncertainty with and without an alternative,
tentative interest, committed replacements, scoped exceptions, rejection,
reaffirmation, unrelated questions/facts, ambiguous/late references, multiple
properties and forged/ineligible evidence. Cases must vary wording and include
scope, negation and mixed question/update counterexamples rather than reproduce
successful phrases.

Two independent human label passes and adjudication must occur before input and
label hashes are frozen. Model inputs must exclude case IDs, family names and
oracle labels. Already inspected cases are development material only. All
existing and future blind holdouts remain outside this slice.

Score property, scope, target, stance, value boundary, evidence and overall
correctness. Match expected assessments one-to-one by identity and target; extra
assessments are false positives, abstention on a required assessment is a false
negative. Non-applicable dimensions are excluded from their success denominator,
which must be reported. Report precision/recall/F1, abstention, false withholding,
wrong binding, critical errors and per-slice denominators. Independent review
must examine outputs, not just whether their quotes appear in source text.

A reviewed critical error blocks promotion. Preserve the counterexample and stop;
run approval does not authorize tuning and repeatedly rerunning until it passes.
These minimum counts help find counterexamples; they are not calibrated
confidence, population accuracy or sufficient evidence for every release slice.
Future measured rates need confidence intervals and denominators, and a separate
production release protocol. Model self-reported confidence is not used.

The next paid run requires explicit approval of reviewed input/label hashes,
provider/model version, request-schema/instruction hashes, call/token limits,
budget/retry policy, output location, synthetic-data sharing and exclusion of
holdouts. Independent blind evaluation and runtime failure/concurrency/load
gates are still required before enablement. None is satisfied by this commit.
