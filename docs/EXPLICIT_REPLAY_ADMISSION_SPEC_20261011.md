# Retained P8: approved bounded durable replay admission

2026-10-11. Originally a read-only proposal; subsequently approved and implemented
as an uncommitted bounded repair. Base HEAD
`4477b839f428f53f787c5a638a7a480dd741e820`. No new frozen requirement.
Regression evidence: `benchmarks/internal/reports/durable-add-replay-repair-20261011.md`.

## Exact boundary

Retained P8 requires exact replay and five-way retry to cause at most one logical
mutation, changed payload/owner under the same identifier to refuse, no false
applied acknowledgement, and recovery within 30 seconds after injected failure
is removed. This proposal addresses durable admission for tenant-bound explicit
preferences. The shared tenant-bound add path also uses this guard for ordinary
requests without source/agent/trusted-mode/prepared-turn identity. This does not
qualify all P8. V2 remains disabled; service-event/agent/MCP/prepared-turn lanes
retain their separate contracts and are outside the durable promise.

## Pre-repair source evidence and limits (historical)

| Source | Established by inspection | Not established |
| --- | --- | --- |
| `memory_service.py:235-257,369-377`; `memory_add_replay.py` | Bound cache hits reject different requests and unbound entries. Cache is written after job creation; missing cache permits admission to continue. | Atomic first admission, durable replay after TTL/outage. |
| `memory_service.py:259`; `_create_extraction_job`; `models.py:2786` | Fresh random job ID on each ordinary admission; job/source-event/proposal creation commits together. No typed tenant/request-key uniqueness exists on job rows. | Five cache misses refer to one job. |
| `provenance_service.py:198-224` | Implicit source `event_id` defaults to the newly minted job ID. | Existing service/event uniqueness deduplicates the application's typed request key. |
| `queue_router.py:171-224` | Redis Lua reserves atomically per tenant/job and refuses on errors; slot release exists. | Reservation per request key; durable binding when Redis is unavailable. |
| `extraction_tasks.py:316-351`; `conflict_resolver.py` explicit preference storage | Worker claims a job under row lock; typed claim/target changes have ownership, version checks and transactional storage. | No repeated logical mutation across distinct jobs or every crash/recovery boundary. |
| `provenance_tasks.py:28-64,69-104` | Redaction preserves operational fields and erases the raw declaration/transcript payload. | Recovering request identity/digest solely from that mutable payload after redaction. |
| `watchdog_tasks.py:26-32,69-145` | Unleased queued jobs have a 60 s grace; watchdog scheduled every 120 s. Existing queued recovery uses conditional row updates. | 30 s recovery after arbitrary dispatch failure removal. This is source evidence, not a reproduced runtime failure. |

The reproduced pre-repair incident was cache-hit metadata misattribution. No
concurrent duplicate-write or cache-expiry incident was executed during that inspection.
Existing claim DEDUP behavior must not be treated as one admitted logical request.

## Options and selected proposal

| Option | Why it is insufficient or selected |
| --- | --- |
| Redis lock/SETNX only | Lock/key expiry, cache loss and commit/cache-write gaps remain; not a durable admission guarantee. |
| Reuse service-event identity or deterministic job ID alone | Source identity participates in authority/provenance; repurposing it is a governance change. A deterministic ID alone does not retain a comparable binding through payload redaction or define recovery. |
| New standalone request service/framework | Unnecessary for this bounded path. |
| Extend existing `ExtractionJob` with durable binding and tenant/key uniqueness | Recommended smallest storage change, conditional on receipt-retention/lifecycle compatibility. Reuses jobs, request digest, status and worker flow; no new endpoint or extraction semantics. |

Propose two nullable fields on the existing job table: a SHA-256 request-key
fingerprint and a versioned request-binding object matching the cache helper.
Require both fields together. Add uniqueness on `(tenant_id, request_key_sha256)`
for bound rows, not `(tenant_id, owner, key)`: an owner change must collide and
refuse. The binding includes effective owner and ingestion/credential fields;
never store raw idempotency keys or transcript in it. Legacy rows remain unbound;
do not manufacture their unknown original keys or silently backfill guarantees.
The owner subsequently approved these two columns, uniqueness constraint and
matching migration. Implemented in `api/db/models.py` and migration
`api/db/migrations/versions/durable_add_replay.py` (revision `durable_add_replay`,
parent `preference_confirmation_index`). No backfill or new table.

## Implemented admission behavior

1. Validate/authenticate the explicit operation using existing policies. Before
   any new queue-slot reservation, look up tenant/key in durable storage and
   compare the binding. Both router fast lookup and service admission must use
   this same identity. A cache hit is an optimization, not authority to declare
   current status or bypass durable validation for the promised path.
2. Existing binding mismatch: conflict with no foreign job metadata. Exact match:
   return the same logical job and its durable status, without a new job, source
   event, proposal or slot reservation. Completion is not an applied receipt;
   keep existing committed-state correlation requirements.
3. For first admission, use a transaction-scoped bounded tenant/key guard
   (existing PostgreSQL advisory-lock pattern), recheck, then reserve a queue
   slot and insert the bound job/source/evidence atomically. The unique index is
   the final integrity backstop. No intervening commit may release the guard.
   PostgreSQL lock wait is capped at 5 seconds; timeout returns retryable 503,
   no job ID and no applied acknowledgement. Five normal concurrent attempts
   converged on one committed job in the regression; no general latency guarantee.
4. Queue full/unavailable before admission: no bound job committed; rollback and
   release any acquired slot. Exact retry can attempt admission after removal
   of the fault. A committed job followed by dispatch/cache-write failure keeps
   its binding; subsequent retry must refer to that job, never create a second.
5. Durable storage unavailable: fail closed before reservation. Missing Redis
   acceptance cache with a durable matching row returns that row. Redis queue
   unavailability still prevents fresh admission under current queue policy.
   Do not introduce a non-atomic queue fallback.

Holding a database transaction during the bounded Redis reservation needs a
finite timeout and exact rollback/release evidence. This introduces no model
call. Hash collision on an advisory-lock key may cause retryable contention;
the full durable key fingerprint/index and binding comparison decide identity.
Regression checks exercised the guard and, separately, forced contention at the
unique constraint, verifying winning receipt retrieval and loser slot release.

## Recovery and retention: do not hide these limits

Durable admission uniqueness is distinct from broker exactly-once delivery and
from one committed preference mutation. Reuse the existing worker claim and
recovery mechanisms, but do not assert they satisfy the 30 s deadline today.
Before implementing any dispatch change, specify a bounded same-job recovery
trigger or approved scheduling change and verify its lease/crash semantics. Do
not silently shorten watchdog intervals or change flags in this task.

Approved lifetime: retain key hash and request binding at least 24 hours after
acceptance and longer while nonterminal, subject to explicit user/tenant deletion.
Existing cleanup redacts payloads but never automatically purges job rows, so the
binding remains with the row indefinitely under that lifecycle. Redaction never
erases the two receipt columns. No new retention or deletion policy is needed.

Protection ends when the receipt/job row is deleted (including existing owner/
tenant cascades). A subsequent request may be admitted again under current
permissions, even if an old Redis entry remains. This is not permanent duplicate
prevention. Legacy unbound jobs have no newly inferred durable replay protection.
Any future automatic job purge must preserve the approved minimum and nonterminal
retention; adding one is outside this repair.

## Regression evidence and remaining qualification

Use synthetic inputs, not private/consumed acceptance sets. Run against an
isolated real PostgreSQL/Redis/worker setup with no paid providers. Demonstrate:

- Five simultaneous exact attempts and retries converge to one durable job,
  one source-event/evidence admission and at most one logical preference mutation;
  inspect SQL claim/revisions/audit/outbox and queue accounting, not response alone.
- Independently varied owner/payload refuses without metadata or mutation;
  exact replay returns original job after acceptance-cache deletion/outage/TTL.
- Failure before commit leaves no binding/job; crash after commit before cache
  or dispatch does not permit a second admission. Replay reads durable status.
- Full queue and slot rollback are correct; restored dispatch/read failures
  converge within retained 30 s or report the remaining P8 failure honestly.
- Raw-payload redaction preserves comparable binding; lifecycle/deletion matches
  the separately approved receipt horizon; unchanged unbound legacy behavior
  remains fail-closed rather than upgraded by assumption.

Completed: 18 isolated real PostgreSQL/Redis regression checks and 59 focused
unit checks. Includes cache deletion/expiry/outage, five-way guard and uniqueness
fallback contention, changed payload/owner, SQL authorization revocation, payload
redaction for 48-hour-old queued/completed rows, receipt/owner/tenant deletion,
queue-full rollback, migration upgrade/downgrade and controlled duplicate worker
delivery. The duplicate checks cover processing and completed jobs, with one
memory and unchanged mutation/audit/outbox counts. Actual HTTP middleware,
asyncpg transport, broker delivery and every worker crash boundary were not tested.
Dispatch fault recovery within 30 seconds remains unqualified and unchanged.

These are development checks, not new acceptance cases, an independent evaluation
or permission to construct the private dataset. The bullet criteria above remain
the broader P8 context, not a claim that all were completed by this admission fix.

## Review checkpoint

The owner approved bounded schema/admission changes, focused isolated regression
checks and matching documentation only. Implementation is complete in the working
tree; review diff/evidence and stop. The frozen candidate must be repinned to a
reviewed code artifact and matching schema before evaluation. This checkpoint
does not authorize deployment, production migration, acceptance evaluation or
additional recovery changes. No full P8 or pilot readiness claim.
