# Trusted preference snapshots: inactive integration slice

`api/services/preference_snapshot_service.py` provides an internal read-only
adapter. The inactive admission service below uses the same reader. No router,
worker, SDK, MCP, assistant, provider, or production flag dispatches these new
services. They do not import benchmark contracts or add another preference writer.

## Input and trust boundary

The backend supplies an authenticated tenant/profile identity, a session factory,
and a `PreferenceSnapshotCatalog` associated with that tenant. The catalog
contains bounded property descriptions and known applicability scopes. It is
validated configuration, not proof of permission and not a model-discovered
namespace. This slice does not implement catalog registration or persistence.

At most 16 properties, 32 scope descriptors, and **32 property/scope identities
in total** may be offered. Duplicate references, duplicate normalized identities,
unresolved applicability, foreign tenant catalogs, and over-limit catalogs fail
before querying. Qualifiers and identities are never silently truncated.

## Exact reads, not vector nominations

The loader makes one SELECT using existing tenant/profile/fingerprint claim
indexes. It starts from the owned profile and joins exact ledger winners,
owned backing-user memories, and matching winner revisions in the same statement.
Blocked profiles, inactive backing users, unavailable targets, inconsistent
values/scopes/categories/states, and malformed declarations fail closed.

Every offered identity gets an immutable result:

- No matching ledger row: absence **within the explicit-preference namespace**.
- Consistent winner: value, memory ID/version, current eligibility, choice state,
  and the maximum of ledger and existing provenance authority.
- Archived, expired, or scheduled winner: present but ineligible, never absence.
- A ledger row with no valid winner (including a deletion tombstone): the entire
  snapshot is unavailable, not partially complete or empty.

Unstructured legacy preferences are not promoted to global defaults or assigned
catalog identities. Namespace absence is not a claim that the user has never
expressed a preference elsewhere. Legacy-path coordination is future work.

The catalog plus all exact identities are bound into an overall digest. Each
identity also has a digest including ledger state, winner revision, authority,
ledger update timestamp, and the existing memory version/current-choice state.
Absent-to-present first writes, authority changes, and hold/reaffirm generations
invalidate the earlier comparison state. Retrieval counters are not added to
the existing memory version definition.

## Connections, limits, and authority

The loader owns a short-lived session, suppresses autoflush, takes no row locks,
and closes the session before returning detached immutable data. It does not
commit or roll back a caller's session, call a provider, embed text, or cache
absence. SQL failures propagate; they do not become empty snapshots.

The bound is on identities/rows and catalog text, not a load-tested latency or
absolute SQL-payload-byte guarantee. Existing memory-version computation still
reads stored memory metadata. No production p99 or high-load claim is made.

Snapshot digests are comparison tokens, not signed attestations, locks, or write
permission. Later mutation must reload and compare state under the existing
identity/target locks, recheck ownership and authority, and use existing audit,
idempotency, and outbox transactions. This slice neither performs those mutations
nor claims that detecting a changed snapshot prevents a race by itself.

It also does not load or validate source transcripts, infer intent, change the
public value-required declaration schema, or turn model confidence into authority.
The snapshot itself does not perform source validation or writes. The separate
internal value-free choice slice below uses this read contract without adding
a live classifier or extraction dispatch.

## Verification

- Unit tests: `tests/unit/test_preference_snapshot_service.py`.
- Real PostgreSQL tests: `tests/integration/test_preference_snapshots.py`.
- Existing preference choice/lifecycle tests remain the writer/read-barrier baseline.

SQL tests use synthetic owners and controlled providers. The new module's SQL
suite forbids real LLM and embedding calls. Tests cover exact scopes, large
profiles, absence, ownership, tombstones, ineligible winners, inconsistent
ledgers, authority, hold generations, concurrent first writes, detached results,
and single-statement/no-lock reads. These are database correctness checks, not
semantic accuracy measurements, live API verification, or release authorization.

## Internal value-free choices (not wired to production extraction)

`ConflictResolver.apply_preference_choices` accepts 1..16 internal requests,
an owned snapshot, the existing backing-user identity and a backend source
conversation UUID. The existing explicit-preference policy flag must be enabled.
No new router, public request field, SDK method or model dispatch calls it.

Each request has only `action` (`withhold` or `reaffirm`), `property_key`,
`scope_ref`, `turn_index` and the actual `quote`. It has **no value, target ID,
authority or confidence input**. The backend binds the exact catalog identity,
claim ID, target ID, memory version and identity digest from the snapshot. Missing
exact targets cannot fall back to another scope. This is an internal contract,
not a permission grant or proof that the quote semantically expresses the action.

Before acquiring row locks, user quotes are checked by the existing source-span
validator, including role and source-kind attribution. In the existing preference
writer transaction, identities are locked in fingerprint order (ledger, memory,
winner revision). The shared snapshot consistency checks, current eligibility,
backing-user/profile ownership and authority are checked again. Both ledger and
provenance authority must fit the existing application-assertion tier (20).
Principal availability is rechecked; this slice does not add permission epochs
or claim linearized concurrent identity revocation across every writer.

Withholding updates only the existing choice barrier and ledger status. Stored
value, memory ID, authority and winner revision are preserved; no memory is
archived/created and no embedding or vector update is needed. Reaffirmation
requires an eligible held winner and restores that same choice. Provider calls
are never made by either operation. This does not establish a measured p99.

An operation digest includes its source conversation, exact source quote/index,
action, identity and original binding. The latest applied operation has a durable
receipt in the choice marker. Its exact retry performs no writes, audits or review
recreation. After another hold/reaffirm generation, older operations fail stale
instead of restoring an earlier choice.
Retries must retain the original snapshot binding and source conversation;
reading a fresh snapshot creates a different operation, not an exact retry.
The durable admission foundation below now preserves these inputs; worker
delivery is still not wired.

Withholding buffers the existing source review using the real user quote and the
post-hold target version, plus a versioned operation descriptor and exact preference
fingerprint. Reviews use the same chat API and retention/capacity policy. Keeping
the stored choice requires that descriptor's exact ledger and hold generation;
dismissal, expiry or purging cannot reactivate the preference. General-context
reads do not expose these scoped reviews.

The internal caller must persist `last_pending_candidates` with the existing
pending-candidate persister, flush audits, and commit **in the same transaction**.
The entry point deliberately does not commit. Unexpected persistence failures
roll back state and audit together. A handled capacity rejection preserves the
hold and its read decision, matching the existing policy.

Tests: `tests/unit/test_preference_choice_service.py` and
`tests/integration/test_preference_choice_operations.py`, alongside the existing
snapshot, declaration, choice and lifecycle suites. They use controlled intent,
synthetic owners and isolated PostgreSQL, with real providers forbidden. Natural
language intent quality, runtime classifier integration, catalog persistence,
legacy coordination and production activation remain outside this slice.

Verified locally on 2026-10-04: 323 preference/review/snapshot and PostgreSQL
checks passed, followed by 450 existing extraction/conflict/clarification unit
checks. The query-plan checks include 1,000, 10,000 and 100,000 synthetic rows;
they do not prove production latency. Ruff passed for the new modules/tests and
the small policy/review services; existing resolver/worker lint findings were
compared with HEAD and no new findings were introduced. Focused mypy passed for
the choice, snapshot and preference-claim services, and `git diff --check` passed.
No paid provider run, holdout execution, production write, commit or deployment
was performed for this slice.

## Durable job admission foundation (inactive)

`api/services/preference_intent_admission_service.py` stores a server-only,
versioned record on the **existing ExtractionJob**, independently of `result`.
The new `preference_intent_admission` migration adds two nullable JSONB columns
(record and optional encryption envelope), with no default or backfill. A 256 KiB
JSONB-text/object constraint applies to new writes without scanning existing rows;
DDL waits at most five seconds for a lock. The service limits canonical UTF-8
records to 240 KiB, retaining headroom for PostgreSQL serialization. Existing jobs
remain null. ORM models also enforce the same constraint when creating a schema.

Migration must run **before deploying code that selects the changed job model**,
even though runtime dispatch is inactive. Downgrading drops admission data and
requires compatible application code; it is not a production rollback instruction.

The internal lifecycle is:

1. `capture`: lock the owned job; read its authoritative persisted payload;
   capture the exact catalog, detached snapshot and full-transcript hash; create
   one original source conversation. Repeated capture returns that same record,
   never a newer snapshot or retry conversation. Changed inputs fail closed.
2. `admit`: validate 0..16 value-free requests and actual user source spans against
   the original capture. The first valid decision is fixed under the job lock.
   Identical retries reuse it; a different decision cannot overwrite it. Zero
   findings is a persisted no-op, not missing work. Invalid decisions leave the
   capture unchanged. `validated` means shape/attribution checked, **not proven
   semantic correctness**.
3. `apply`: recheck principal, policy, source and input bindings; use the existing
   choice writer, audit and pending-review persister in one transaction. Changes
   to the target are rejected under existing identity/target locks, not rebound.
   The terminal receipt commits with those writes. Review/encryption/commit
   failure rolls back the entire operation; retry uses the admitted decision.

The receipt survives ordinary job completion overwriting `result`. A terminal
retry returns the historical outcome without applying it again, reopening a
review or undoing a newer choice. **A receipt is not the current preference**;
currentness still comes from the governed read path.
Completed/dead jobs cannot admit or apply an unfinished decision. Returning an
already-terminal receipt is read-only and remains allowed if ownership, source
retention and policy checks still pass.
Handled review-capacity rejections retain the hold, consistent with existing
policy. A batch outcome
explicitly records applied/deduplicated/rejected counts; it is not a promise that
all identities will be eligible.

Each stage owns one short transaction and one database connection. Capture uses
the factored snapshot reader inside its transaction, with no nested pool checkout.
The reader neither commits nor closes that session. No model/network call occurs
while these transactions are held. Job locks serialize same-job deliveries; sorted
identity/target locks remain the writer's existing order. This is not a measured
production p99, pool-capacity sizing result, provider exactly-once guarantee or
permission-epoch implementation.

Source input is bounded to 64 turns and 64 KiB of canonical UTF-8 JSON. Full turns
are hashed, including role, source kind, turn IDs and qualifiers; text is never
silently truncated. Agent-bound, explicit-source and manual-declaration jobs are
excluded from this foundation. No-match, malformed, expired, redacted, blocked,
foreign-principal or changed-input records cannot be reset into a fresh admission.
There is no regex pre-filter or semantic classifier here.

The record contains source quotes and stored values, so it is private data:
public job-status allowlists do not expose either new column. It follows existing
tenant/job-bound envelope **dual-write** configuration, not ciphertext-only storage.
Encryption failure aborts persistence. Existing raw-job retention clears both
record and envelope; expired/redacted jobs cannot be recaptured. This service
does not independently establish a new retention duration.

Unit contract tests: `tests/unit/test_preference_intent_admission_service.py`.
PostgreSQL tests: `tests/integration/test_preference_intent_admission.py`.
They exercise controlled decisions, crashes/retries, competing deliveries, stale
bindings, ownership, a one-slot pool, completion overwrite, encryption rollback,
retention, database bounds and public status privacy. Real providers are forbidden.

Still outside this foundation: catalog registration/persistence, semantic
classifier/worker dispatch and legacy-path coordination, live quality evaluation,
SDK/MCP/assistant changes and production activation. No live provider run or model
accuracy claim is authorized by these database tests.

Verified locally on 2026-10-04: 384 combined governance/PostgreSQL checks and
479 existing extraction/conflict/lifecycle unit checks passed. After adding the
closed-job guard and reaffirmation case, the final focused suite passed 85 checks
(these suites overlap; their counts must not be summed as unique coverage).
The empty-database migration upgrade and a downgrade/upgrade round trip passed
on disposable PostgreSQL. Both columns remained nullable without defaults, and
the deferred-validation constraint was present and enforced on new writes.
Ruff and focused mypy passed for the new services/tests/migration; the model and
retention task's existing lint findings were unchanged from HEAD. No provider
calls or production changes occurred. The frozen v4 evaluation journal hash
remained unchanged. These checks preceded the scoped foundation commit; no
production deployment or activation was performed. The snapshot, value-free
choice and admission PostgreSQL suites are included in the existing CI
governance integration gate.
