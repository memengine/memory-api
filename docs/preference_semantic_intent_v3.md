# Offline preference intent v3

This experimental contract separates source meaning from backend candidate
routing. It is not imported by production and contains no provider, SQL or write
adapter. The separate [v3 development preparation](preference_intent_development_v3.md)
seals a fixed offline runner policy; its preparation does not approve execution.
No SDK/MCP/assistant change or deployment is included. V1/v2 contracts,
freeze manifests and consumed results stay unchanged. Old responses are invalid
v3 input, not material to repair or relabel.

## Interpretation

Exactly six fields are required: `kind` (`assessment`), `property_key`,
`scope_ref`, `intent`, `evidence` and nullable `value`. Evidence and value reuse
the existing turn-index/exact-quote types; adoption/change require a value of at
most 256 characters quoted within the evidence on the same eligible user turn.
Assistant options can supply reference context, never user value evidence.

| Intent | Source meaning, not a database operation |
| --- | --- |
| `adopts_value` | Adopts a persistent current value without explicitly replacing an earlier choice |
| `explicit_change` | Explicitly corrects/replaces an earlier choice; remains this intent even if no matching stored target exists |
| `current_choice_unresolved` | Says the current choice is undecided, not merely that another option is interesting |
| `tentative_interest` | Considers an option without adopting it |
| `reaffirms_current` | Explicitly keeps the current choice |
| `rejects_alternative` | Rejects an option; does not thereby adopt or reaffirm a different one |
| `historical` | Describes a former preference, not a new current adoption |
| `unclear` | Preference-related meaning without a defensible stronger interpretation |

Model output cannot include a target reference, ID, version, route, authority,
confidence, completeness declaration or activation instruction. Extra fields
are rejected. Existing five abstention reasons are reused. Input projection
reuses v2: full role-labelled source, offered properties/known scopes and existing
values/choice states, excluding backend IDs, authority and coverage. This is
field exclusion, not redaction of arbitrary source text. The generated schema is
standard JSON Schema only; no provider-dialect compatibility has been measured.

## Backend-owned candidate routes

The backend preserves the supplied intent/evidence/value, binds the unchanged
exact identity and version, then derives a candidate route. All results carry
`automatic_activation_allowed=false`; even a replacement candidate is not write
permission, calibrated confidence or proof that the model interpreted correctly.

| Meaning and trusted state | Candidate route |
| --- | --- |
| Adoption + verified absence | `new_candidate` |
| Adoption + same current value | `duplicate_candidate` |
| Adoption + different value or existing hold | `review_required`; never infer replacement or clear a hold |
| Explicit change + permitted current target + different value | `replacement_candidate` |
| Unresolved current choice + permitted current target | `withhold_candidate`, with no fabricated alternative |
| Explicit reaffirmation/change + held target + same value | `reaffirm_candidate`; value-free explicit reaffirmation is also representable |
| Explicit change/unresolved/reaffirmation + absence | `review_required`; preserve intent rather than relabel or invent a target |
| Change/withhold/reaffirmation + target authority above 20 | `review_required`; never route a stronger target to mutation |
| Named reaffirmation inconsistent with stored value | `review_required` |
| Tentative interest, rejected alternative or history | `no_current_change`; does not clear an existing hold |
| Unclear intent | `review_required` |

Values are compared exactly, without case folding, alias guessing or embeddings.
An unchanged non-current target rejects rather than becoming absence. Project
identities never bind broader global targets. Unknown scope/property abstention
does not guess a different identity. Current choice uncertainty may be value-free;
this module does not convert it into the value-requiring public declaration API.

## Attribution, currentness and limits

Reuse the v2 complete snapshot wrapper and its ownership/uniqueness checks, plus
the production source-span validator and source-kind policy. Snapshot copies are
revalidated. Principal/block, catalog/source changes, incomplete coverage, stale
matching presence/absence/version/authority/state, invalid spans and duplicate
identities reject the entire batch without partial proposals or writes. Tool,
document and assistant content cannot supply evidence through an embedded role
claim. `client_assertion` stays that source kind, not user attestation.

Coverage remains a trusted fixture assertion, not proof of a complete SQL query.
Comparing snapshots is not transactional race protection. Bounds remain 16
properties, 32 scopes, 32 targets, 64 turns, 8,000 source characters, 32,000 context
characters, 512 covered identities and 16 findings. Indexed binding and one span
check per finding add no network calls. These are work bounds, not measured
latency, throughput or a production consistency guarantee.

The route preview is separate from the existing production transition function:
it does not fabricate `PreferenceClaimRequest`, invoke a writer or implement a
second authority system. A future reviewed adapter must reuse the existing
transaction, identity reservation/lock order, writer permission and fresh version
checks. Complete database snapshots, value-free hold admission, temporal ordering,
retry/idempotency behavior and read consistency still need integration design.
Keep model interpretation outside the immediate answer path; asynchronous
processing alone cannot guarantee immediate read-after-write currentness.

## Verification boundary

Run `.venv/Scripts/python.exe -m pytest tests/unit/test_preference_semantic_intent_v3.py`.
Also run the frozen v1/v2 and existing diagnostic tests to verify unchanged hashes
and contracts. Tests supply interpretations, including multilingual text; they
do not measure natural-language recognition or replay any consumed run. No
private holdouts or review worksheets are loaded. Before a paid evaluation,
review this contract, separately prepare/seal the request/schema and approve a
new run. Independent semantic evaluation, calibration and production release
gates remain unmet.
