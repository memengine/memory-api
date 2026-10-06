# Explicit preference contract v1 — local, disabled by default

This is the first bounded implementation of typed preference applicability.
It is **not** automatic scope extraction, a general semantic conflict detector,
or automatic assistant behavior. Local, unreleased Python/TypeScript SDKs now
expose the optional fields; no production enablement or SDK release is included.
`EXPLICIT_PREFERENCE_CLAIMS_ENABLED` defaults to false;
requests using this contract then receive `409 explicit_preferences_unavailable`.
Keep it disabled in production until the integration/release gates are approved.

## Declaration and trust boundary

The existing authenticated tenant `POST /v1/memories/add` accepts an optional
`preference_claims` array (1–16). When present, the job processes only those
declarations, not a second model extraction of the transcript. Normal clients
that omit it keep the existing flow. This v1 path does not accept registered
source events, agent-bound writes, or the server's MCP submission lane.

```json
{
  "external_user_id": "customer-owned-stable-id",
  "messages": [{"role": "user", "content": "For Release Check, use Python for examples."}],
  "preference_claims": [{
    "property_key": "examples.language",
    "value": "Python",
    "applicability": {"kind": "context", "namespace": "project", "key": "release-check"},
    "turn_index": 0,
    "quote": "For Release Check, use Python for examples.",
    "commitment": "asserted"
  }]
}
```

Applicability is explicitly `global`, `context` (namespace + case-sensitive
stable key), or `unresolved`. There is no text-derived project name, pronoun
normalization, or unknown-to-global fallback. Property keys are application-owned
stable identifiers, not a hardcoded list of programming languages. Changing a
key creates a different identity; clients must keep their key vocabulary stable.
One job may declare at most one value per property/applicability identity.

The backend reuses the existing evidence validator: the indexed turn must be
eligible user input; the exact quote must occur uniquely; the value must occur
verbatim in the quote. Tool/document/assistant/system evidence is rejected.
This verifies quotation and attribution within the submitted transcript, **not**
that an external application's role labels are attested, that the quote entails
the value, or that its declared applicability/commitment is linguistically true.
For example, a bad application could label a negative statement as a preference.
That is why declarations are `client_asserted`, capped at authority **20**, even
when the API key's ordinary source writer has higher authority. A declaration
cannot grant permissions, raise authority, or overwrite an unrelated ledger key.
Metadata cannot substitute for this typed contract. The 0.5 confidence field is
a neutral compatibility value, not calibrated semantic confidence or a gate.

## Deterministic transitions

Identity is `(tenant, profile, preference-v1 namespace, property_key, applicability)`.
The value and source wording are not part of the identity. Existing text-derived
ledger identities remain separate; no historical backfill labels them global.

- A new known identity becomes current at application-assertion authority.
- An identical value in that identity is a no-op; no extra memory/revision.
- Different known applicability creates a separate memory, never a merge.
- Different value without an explicit replacement stays pending.
- `uncertain` commitment defaults to `choice_state: "candidate_only"`: it stays
  pending without withholding an existing default. `unresolved` applicability
  also stays pending, with no guessed scope.
- `replacement` additionally requires `replaces_memory_id` and
  `replaces_version`. The target must be the current, owned winner of the exact
  identity, its version must match, and its authority must not exceed 20.
  Only that target is superseded. Stale/foreign/wrong-scope references are pending,
  not permission to replace another memory.

The replacement version is returned as `provenance.preference_version` on a
context-aware retrieval result. It is server-derived response data, not a stored
provenance mutation. It binds the current memory content, ownership, applicability,
authority metadata and validity using the existing source-review version helper.

The path reuses the resolver's memory/version/audit/vector-outbox writes and the
existing claim ledger. PostgreSQL's existing unique owner/fingerprint index plus
`INSERT ... ON CONFLICT DO NOTHING` and row locks serialize first-writer races.
Batch lock order is sorted by fingerprint. All required embeddings happen before any
claim locks; no extraction/conflict LLM or vector nomination runs in this path.
Pending candidates also use exact declaration/snapshot fingerprints with an
indexed insert-and-lock path: no fuzzy full-category scan, cross-context
reinforcement, or concurrent duplicate insertion in this mode.
Memory, predecessor archive, ledger winner/revision, audit and outbox are in one
SQL transaction. Ledger recording errors abort it instead of being swallowed.
The source conversation may already exist after failure; it grants no memory
authority. There is no new table, index migration, or duplicate memory store.

Legacy conflict nomination excludes declared preferences. Generic content edits
or unarchiving of a declared preference are rejected; use version-bound replacement.
Importance edits and deletion/archive retain the existing authorized operations.
Soft archive is a tombstone: ordinary assertions against it remain pending;
generic unarchive is not supported. Archive/deletion lock ledger then memory,
the same order as replacement, and archive the current ledger/revision without
clearing its target reference or authority. Archiving an old superseded revision
does not change its successor. Archived preferences never enter scoped context;
an applicable global preference can resume as fallback unless a durable
unresolved-choice barrier exists for that scope (see below).

An intentional `replacement` can renew an archived winner with its exact ID and
post-archive version, unchanged ownership/applicability and authority checks.
Renewal creates a new memory linked to the archived predecessor, even for an
identical value; it never silently unarchives the old row. Existing authorized
get/list/update responses also expose response-only `provenance.preference_version`,
including archived targets. Hard deletion reconciles claims without resurrecting
superseded revisions. An archived target that is no longer the ledger's winner
cannot be renewed. No automatic authority reset or linguistic inference occurs.

## Context-aware reads

The existing `POST /v1/memories/retrieve` accepts:

```json
{
  "external_user_id": "customer-owned-stable-id",
  "query": "Which language should examples use?",
  "categories": ["preference"],
  "preference_context": {
    "applicability": {"kind": "context", "namespace": "project", "key": "release-check"},
    "property_keys": ["examples.language"]
  }
}
```

The integration names 1–16 relevant property keys. One indexed SQL query reads
at most 32 identities (requested context + known global). For each property the
current context-specific winner suppresses only that property's global winner.
If no applicable context-specific preference exists, a known global declaration
can apply. Inconsistent ledger/target state blocks fallback rather than guessing.
Other requested categories use the existing retrieval path. Requested preferences
are placed before those results, within the existing result limit; their relevance
score is a compatibility ranking score, not a semantic probability.

Preference winners bypass caches and are freshly owner/tenant/block/validity
checked on every explicit read, including degraded mode. Other categories can
still use the existing cache. Blocked/passthrough quota modes return no context.
No read-time model call or full-profile scan is needed for preference selection.
There is no measured production p99/high-load capacity claim yet; the batch/SQL
bounds and concurrent regression tests are not a load benchmark.

By default, reads without `preference_context` exclude declared preferences in current,
cached hydration, cold-start, PostgreSQL fallback, and historical candidate paths.
They retain legacy behavior for legacy records. Explicit reads exclude unknown
legacy preferences instead of relabeling them global. Scope-aware `as_of` reads
are explicitly rejected in v1, not silently answered using current scope. Domain
overlays and legacy unscoped clarifications are not mixed into explicit reads.
Pending source reviews are filtered by requested property/applicability before
the list limit; other projects' reviews do not leak into this chat. Reviews match
the exact requested scope, not the global read fallback, so a project chat does
not silently ask the user to change a global preference. Unresolved
applicability is persisted pending but not auto-delivered into a guessed context.
Review buttons do not activate declarations; restating does not infer their scope.
A matching unresolved-choice review can explicitly reaffirm the already stored
value under the conditions below; it never activates its candidate text.

### Ordinary discovery reads (default-off, source checkout)

An operator may separately enable `RUNTIME_PREFERENCE_DISCOVERY_ENABLED` for an
existing allowlisted tenant with explicit preference claims and a valid operator
catalog. Ordinary current reads then discover keys from bounded existing search
nominations and check current owned SQL claim bindings. No query wording or model
is used to choose application scope. Unknown legacy preferences are withheld in
this mode; unambiguous declared globals can be returned using the existing selector.

If a nominated property has current scoped bindings, a request lacking application
scope withholds its value and returns this additive read response:

```json
{
  "preference_scope_request": {
    "state": "application_scope_required",
    "property_keys": ["examples.language"],
    "available_scopes": [
      {"kind": "global"},
      {"kind": "context", "namespace": "project", "key": "Cedar"}
    ]
  },
  "clarification_question": "Which application/project scope applies to these preferences? Please choose a supplied scope; no default has been selected."
}
```

Global may be offered as an explicit choice; it is never silently selected when
scoped bindings exist. Surface the question in the customer's existing chat and
send the application-owned or explicitly user-selected scope and returned keys as
`preference_context` on the next read. Scope selection itself does not write a
preference, increase authority, or grant access. It does not have a separate answer
endpoint or authorize mutation. The existing unresolved-choice policy still applies
after a scope is supplied. Unaffected global properties and eligible facts can remain
in the response. Typed scope requests and decisions are independent of context budget;
budgeted notices accompany them when room exists. Held runtime processing takes
priority over questions and is not a request for user clarification.

Bounds: 50 nominations, 16 property keys, 32 owned scope bindings plus one overflow
sentinel, 32 offered known scopes. Removed catalog identities, corrupt bindings and
overflow yield unavailable context, not guessed global fallback. The new online
`preference_discovery_index` migration supports prepared predicate-key probes; it
must be deployed before activation. Search misses remain a recall limitation.
Neither this bounded read contract nor local query-plan checks prove semantic
accuracy, production p99, or launch readiness. There is no read-time classifier,
automatic polling, worker wait, new UI, or MCP/universal contract change.

## Durable unresolved current choice — local, unreleased extension

Tentative interest in another value is not an unresolved decision about the
current default. The backend accepts an explicit declaration of the latter:

```json
{
  "property_key": "examples.language",
  "value": "Python",
  "applicability": {"kind": "context", "namespace": "project", "key": "release-check"},
  "turn_index": 0,
  "quote": "For Release Check, I am undecided between my current C++ default and Python.",
  "commitment": "uncertain",
  "choice_state": "current_choice_unresolved",
  "replaces_memory_id": "00000000-0000-0000-0000-000000000001",
  "replaces_version": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
}
```

The example ID/version are placeholders: use the actual owned winner and its
current `provenance.preference_version`. This operation requires an existing
declared winner, known exact applicability, eligible quoted evidence, and both
ledger and stored-memory authority at most 20. Missing, foreign, stale,
wrong-scope, archived, expired, or future targets cannot be withheld. Rejections
create neither an active memory nor a review containing the rejected text.
They appear as bounded reason counts in job metadata:
`extraction_metadata.preference_choices.{withheld,reaffirmed,rejected}`.

Withholding keeps the old content, authority, winner and revision as history;
it does not archive the memory. The existing ledger becomes `disputed`, and a
backend-owned marker on the winner records the durable choice state and a new
generation. That generation changes the target version. Marker, ledger, audit
and any retained review share the existing transaction. A persistence failure
rolls them back; deliberate review capacity rejection does not roll back the
choice state. No extra table, duplicate storage, or migration is required.

Scoped reads omit that property's withheld value and do not substitute a broader
global default. Other properties and scopes remain independently usable. The
response adds at most 16 `preference_decisions`, each containing only
`property_key`, `applicability`, and `state: "current_choice_unresolved"`, and
sets the existing `clarification_question`. No withheld value is added to usable
`data` or prompt context. Management/history and a retained review can still
show it explicitly as stored history. Decisions persist even when a review is
dismissed, expires, is purged, or cannot be retained due to capacity limits.
Soft archiving a withheld winner also does not silently restore global fallback.
Explicit authorized hard deletion still uses existing deletion reconciliation;
this is not a permanent barrier surviving deletion of its owning memory.

An explicit version-bound `replacement` with the same value can reaffirm a
still-valid withheld winner without creating another memory or raising authority.
A different value creates the ordinary replacement. Renewal of an archived or
expired held winner creates a fresh memory, never reactivates the invalid row.
Alternatively, `keep_current` on the matching, still-valid review reaffirms only
the existing value. Ownership/block status, target/review versions, currentness,
authority, and the hold's evidence binding are rechecked under locks. `restate`,
`dismiss`, ordinary duplicate assertions, unrelated tentative reviews, stale
reviews and old queued jobs cannot clear or replay the choice state. Each new
hold or reaffirmation rotates the version binding. Concurrent answers encountering
a locked target/ledger receive the existing retryable `409` review conflict.

The local, unreleased Python/TypeScript SDKs now expose this contract:
`choice_state` / `choiceState` on declarations, and typed
`preference_decisions` / `preferenceDecisions` on tenant retrieval results.
Existing SDK parsing of `clarification_question` and raw job metadata remains
compatible. Older server responses without decisions yield an empty list; old
calls retain their previous wire shape. The SDKs do not silently retry failed
declarations without choice fields. Decisions are independent of whether usable
context exists: check them even when `has_context` / `hasContext` is false.
Do not claim that published packages already contain these local changes or
that a customer agent must display or obey a question. SDK publication, customer
integration validation and semantic admission are separate release gates.
Quotation is still attribution, not semantic proof: no classifier was added to
decide whether free-form language truly means an unresolved current choice.

There is no read-time provider call or additional SQL lookup for the choice
decision: it uses the already bounded ledger/winner result. Uncertain declarations
do not need embeddings. Writes remain queued; an answer arriving before a queued
declaration commits can still observe the preceding state. This slice introduces
neither a read-after-write guarantee nor measured production p99/load results.

## Verification and remaining gates

Local tests cover strict schemas, evidence forgery/quote mismatch, stable identity,
project/global coexistence, exact-scope replacement, deduplication, stale targets,
authority bounds, uncertainty, tenant isolation, concurrent first writes and
replacement races, legacy candidate/cache exclusion, scoped review delivery,
and transaction rollback on a ledger failure. Provider responses/embeddings in
these tests are controlled; they do not measure language understanding.

Still separate work: SDK release and customer integration validation, scoped historical
reads, agent-bound identities, automatic property/applicability/commitment
admission, independent semantic evaluation, and load/latency measurement.
Local review lookup checks now cover 1K/10K/100K synthetic rows, one and sixteen
property keys, legacy and empty-result reads, including forced generic prepared
plans for scoped lookup. Fixed policy predicates/JSON paths remain SQL constants;
caller identity and property values remain bound parameters. A targeted partial expression index
reduced buffer work at 100K rows; an additional partial age index supports bounded
background expiration/retention. These checks are isolated SQL plans, not loaded
production p99, provider latency, or throughput measurements. See the review
contract for deadlines, storage caps and explicit capacity-rejection outcomes. No
customer agent prompt or new UI was changed to make these tests pass. MCP,
assistant and universal-path changes remain outside this slice.

## Local SDK contract (unreleased)

Python `Memory` and `AsyncMemory` accept keyword-only `preference_claims` on
`add()` and `preference_context` on `get()`. Both accept their exported typed
models or dictionaries. TypeScript adds an `add(AddRequest)` object overload
with `preferenceClaims`, while retaining the existing positional signature.
Its existing `get(GetParams)` object form accepts `preferenceContext`. These
are additive opt-in fields: omitted fields are not sent and old calls keep
their previous wire shape. Python models are standalone, not imports of the
backend package; schema parity tests protect against drift.

The SDKs reject source/agent-bound declarations and agent-bound/historical
explicit reads before sending. The backend remains authoritative for evidence,
ownership, permissions, currentness, authority and all admission decisions.
TypeScript types describe valid shapes but are not a runtime trust boundary;
the backend validates lengths, keys, quotes and replacement references. No SDK
infers property keys, applicability, commitment, or trust from natural language.
When the server flag is off, clients surface `409` with code `PREF_409` and error
`explicit_preferences_unavailable`; neither retries without the preference field.

`add()` submits one queued job and does not automatically wait for it. If a caller
needs confirmation of asynchronous processing, it can separately use existing
`get_job_status()` / `getJobStatus()` or explicitly poll. A completed job means
processing completed, not that every declaration became current or every review
was retained. Check created IDs, pending counts and extraction metadata.
`job.source_reviews_capacity_rejected` (Python) /
`job.sourceReviewsCapacityRejected` (TypeScript) reads the existing diagnostic,
defaulting to zero for absent or invalid counts; raw metadata stays intact.
Uncertain declarations can remain pending with no created memory ID. Review
capacity rejection is not storage or authority escalation. Neither outcome
accessor provides a new per-declaration disposition API.

Retrieval and management responses preserve `provenance.preference_version`
and unknown provenance fields, including an archived version when returned by
the authorized management API. Copy the exact backend ID/version into replacement
declarations; do not fabricate a version or take it from assistant text. Versions
can become stale concurrently; a failed replacement is not silently retried with
new authority. Legacy reads exclude declared preferences; continue using the
explicit context when retrieving declarations.

Keep extraction/polling off the answer-generation critical path unless the
customer explicitly needs processing confirmation. This contract introduces no
read-after-write guarantee: an immediate read can precede job completion.
Transport and schema tests cover compatibility, not live chat behavior, language
understanding, measured production p99 or loaded capacity. No publication,
package-version bump, feature enablement, assistant rules, or new UI is included.
