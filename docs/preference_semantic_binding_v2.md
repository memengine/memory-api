# Offline preference semantic binding v2

This version changes who chooses a stored target: interpretation proposes a
property/scope; the backend binds an exact matching target. It has no provider,
SQL client, production import, write adapter, SDK/MCP/assistant change or feature
flag. Outputs remain proposals with `automatic_activation_allowed=false`.
V1 and every consumed experiment remain unchanged. There is no permission to
rerun those experiments or to execute a v2 provider evaluation.

## Model output versus backend output

An interpretation contains exactly six required fields: `kind`, `property_key`,
`scope_ref`, `stance`, `evidence`, and nullable `value`. Stances, quote/index
bounds and abstention reasons reuse the frozen v1 types. Asserted and replacement
interpretations require a separately quoted value. No `target_ref`, memory ID,
version, completeness flag, authority or activation instruction is accepted from
the model; extra fields are rejected, not silently removed.

Input reuses v1's catalog, known scopes, role-labelled full turns and source
eligibility pre-filter. Existing preference values and choice states remain
available as semantic context, without target references. Owner IDs, memory IDs,
versions, authority and coverage declarations never enter the structured model
projection. This is field exclusion, not redaction of arbitrary text in source
content. The generated output schema is standard JSON Schema, not a certified
provider dialect. No serializer or prompt modification is included.

## Explicit completeness and exact binding

`BindingSnapshot` wraps the existing bounded `ContractContext`. Its trusted
supplier must enumerate `covered_identities`, including identities proven absent,
not just targets returned from a query. Default empty coverage is incomplete.
Coverage must be unique, inside the offered catalog, and include every offered
property/scope pair (at most 16 × 32 = 512). Incomplete input produces no model
projection; a supplied reply against incomplete shown or current coverage is
rejected. Snapshot construction and validation recheck the existing context
ownership, uniqueness and size bounds, including multiple-winner rejection.

This pure contract cannot prove the supplier queried a database correctly. The
coverage list is a backend/test-fixture assertion, not attestation and not a
model-controllable field. A future production supplier needs a complete SQL view;
vector top-k results, a bounded public retrieval result or a cache miss cannot
establish absence. Do not label a partial query complete or drop overflow rows.

For each interpretation the backend indexes targets by exact `(property_key,
scope_ref)` inside the authenticated tenant/profile context:

| Observation in shown and current snapshots | Result |
| --- | --- |
| Both prove no matching target | Bind null; only stances not requiring an existing target can proceed |
| Same owned matching target, including version, authority and choice state | Bind that target's reference; apply frozen validation |
| Matching target appears, disappears or changes | Reject `binding_target_changed`; never bind the latest winner automatically |
| Matching target is not current | Reject through the frozen eligibility gate; never reinterpret a tombstone as absence |
| Coverage is incomplete, ownership invalid or winners ambiguous | Reject; no partial assessment |
| Only a broader global target exists for a project identity | Do not bind it as the project target |

Principal/block, catalog and source changes also reject the batch. Bound findings
go through the unchanged v1 validator for evidence, duplicates, scope, target
currentness and authority. Output `target_ref` is backend-derived, not something
the provider selected. A supplied v1 reply carrying a wrong target is invalid v2
input; it is never coerced into a successful result.

For the recorded Python adoption case, `(examples.language, global)` has no
matching target. Complete unchanged snapshots therefore yield a null binding;
the unrelated `(answer.detail, global)` memory is not considered. This is a
deterministic contract example, not a new model response or a semantic score.

## Binding is not permission

The binding step does not change stance, source trust or authority. An asserted
different value does not become a replacement. Higher-authority targets retain
the frozen restrictions for replacement, unresolved-choice and reaffirmation.
Harmless proposal-only findings may still refer to a higher-authority target;
none may write it. Even a bound proposal passing every structural check can be
semantically wrong (negation, scope or stance), so independent evaluation remains
mandatory. There is no regex intent recognition or model-confidence gate.

Future admission must reuse the existing preference fingerprint/ledger policy,
owner checks, lock order and version-bound transitions. It must not duplicate the
store or treat model stance as permission. The public declaration schema cannot
represent a value-free unresolved finding; this version does not fabricate an old
value, convert to that schema, or implement an internal mutation adapter.

## Bounds and release boundary

Existing bounds remain 16 properties, 32 scopes, 32 targets, 64 turns, 8,000 source
characters, 32,000 context characters and 16 findings. Explicit coverage adds at
most 512 identities outside the model input. Binding indexes the bounded targets
once; it adds no model call, vector search or database operation. These are work
bounds, not measured production latency or load capacity.

The existing scope-aware SQL read covers at most 32 fingerprints for one request
context plus global defaults. It is not a complete snapshot supplier for every
possible v2 catalog. Building that supplier and transactional absence/version
guards requires separate review; typed offline snapshots are not race protection.

This version is frozen alongside its protocol and the v1 dependencies. Unit tests
verify deterministic binding and safety, not language understanding. No AI draft
annotations or private holdouts are loaded. Production routing, asynchronous
admission, read integration, catalog registration, calibration, load testing and
human release gates remain pending. Interpretation must remain outside the
answer's critical path when a future production adapter is designed.
