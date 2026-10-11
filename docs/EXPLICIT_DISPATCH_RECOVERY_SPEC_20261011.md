# Bounded same-job dispatch recovery specification

2026-10-11. Specification approved after one reproduced diagnostic; the owner
subsequently approved and implementation completed in the working tree.
Source candidate: `4bf8884737ef58416bb34b9d831b0836333d21c0`.
Focused local regression evidence only; no independent pilot evaluation.

## Problem and exact scope

An admitted durable job remains queued when the API is interrupted after SQL
commit and before dispatch. The single synthetic diagnostic observed no dispatch
at 30.265 seconds after fault removal. Receipt uniqueness held; zero mutations.
Evidence: `benchmarks/internal/reports/dispatch-recovery-diagnostic-20261011.json`.

Repair only committed durable admission before worker start. Reuse ExtractionJob,
current receipt binding, queue reservation, compact job reference and existing
worker claim/mutation safeguards. Do not recover terminal jobs, take over processing
jobs, reset worker attempts, add receipt storage, change deletion or change semantics.
Only explicit-operation evidence may support the pilot; use of the shared admission
path by ordinary requests creates no interpretation promise.

## Necessary trigger decision

An API background task or replay-only dispatch does not survive API death or work
without a client retry. Existing watchdog excludes fresh jobs for 60 seconds and
runs every 120 seconds. It cannot meet retained 30 seconds for this gap.

Propose one distinct five-second dispatch-recovery task in **existing Celery Beat**,
serviced by existing workers, reading existing job rows. No new service, table,
endpoint or broker. This is an additional scheduled trigger and therefore needs
explicit implementation approval; it is not already covered by admission approval.
The watchdog's 60-second grace/120-second schedule remain untouched. If a new
trigger is not approved, full crash-independent recovery cannot be promised for
this gap under the unchanged schedule. Do not weaken P8 or declare readiness.

## Proposed algorithm and safety boundaries

1. For newly admitted durable jobs, choose a stable Celery delivery ID and persist
   it in existing `celery_task_id` in the same transaction as admission. Include
   that ID in the job payload used for initial publication. For existing bound
   queued jobs with no ID, conditionally assign it under the SQL row lock; retain
   an already stored ID. Never change the ID of a processing job.
2. Initial dispatch publishes the authoritative compact job reference with that
   exact ID. Status/acceptance-cache writes cannot gate dispatch or determine its
   admission outcome. A publish failure on this bounded durable path leaves the
   same row queued for recovery; it creates neither a second admitted job nor an
   extra queue reservation. Existing behavior of excluded lanes stays unchanged.
3. Each recovery cycle scans bound `queued` rows with `processing_started_at IS
   NULL`, including rows with a recorded delivery ID. A recorded publish ID is
   not proof of delivery or worker start. Select a bounded oldest-first batch
   using `FOR UPDATE SKIP LOCKED`; reload status/owner/tenant availability before
   publication. Deleted/inactive/blocked identities must not be resurrected.
   Reuse current worker authorization and mutation checks; do not introduce a
   new authority policy or interpret request text.
4. Serialize recovery publication per row with its SQL lock and recheck eligibility.
   Send only the stored reference/queue and delivery ID. Use bounded SQL and broker
   operations; do not keep transactions indefinitely open. Publishing may repeat
   when acknowledgement is lost or a recovery process dies; every copy references
   the same row/ID. This is at-least-once delivery, not exactly-once transport.
5. Worker entry already locks the row, checks queued/failed status and stored task
   ID, then marks processing. Reuse this unchanged claim for identical-ID copies;
   processing/terminal copies must be ignored. Do not rely on Celery result-ID
   deduplication. Once a worker starts, this recovery task leaves it alone.
6. Existing reservation belongs to the admitted job. Recovery never reserves a
   new slot or releases that slot solely because another delivery was published.
   Existing completion/failure cleanup remains responsible. SQL deletion ends
   receipt protection; no tombstone or reconstructed job after deletion.

Implementation review must establish the publisher's actual finite timeout and
row-lock interactions with initial dispatch/worker start. Reusing `celery_task_id`
is feasible in source because the worker compares it before taking its claim.
If source review reveals that safe implementation needs new storage or a changed
worker mutation/deletion policy, stop and present that decision; do not expand.

## Timing and evidence required before any claim

Proposed pilot budget from fault removal to observed terminal outcome:
<=5 s recovery-trigger delay (including measured scheduling lag), <=3 s bounded
SQL/publication, <=20 s worker queue/start/operation, <=2 s observation = <=30 s.
These are proposed measured budgets, not established capability or a replacement
for frozen P8. Five-second Beat scheduling alone guarantees none of them. Bound
the approved pilot workload and measure actual scheduler/queue behavior; if those
budgets cannot hold, report failure rather than mark a queued receipt applied.

After separately approved implementation, focused synthetic verification must
cover the reproduced exception and actual API termination after commit, publisher
termination before/after broker send, lost publish acknowledgement, concurrent
recovery/initial publication, processing/terminal duplicates, deletion and revoked
identity. Inspect job/source/reservation/claim/revision/audit/outbox state. Include
at least one real local Beat/broker/worker timing check without paid providers;
manual task invocation cannot establish scheduled recovery. No private cases or
new acceptance dataset. Preserve the original diagnostic artifact; a repaired
candidate needs a new artifact and repin, not overwritten historical evidence.

## What this proposal does not solve

It does not establish recovery of a worker already processing, a crash between
memory commit and job completion, arbitrary queue overload, scheduler outage,
provider failure or all P8 invariants. Those remain separate current limitations,
not authority to investigate or repair them in this scope. The reproduced failure
authorizes a proposed repair decision, not a general patch-and-rerun loop.
Source already sets a 10-minute processing stale timeout in
`extraction_tasks.py:61,341` and task soft/hard limits of 105/120 seconds in
`celery_app.py:85-88`; this proposal leaves those unchanged. Consequently it must
not be described as satisfying 30-second recovery for all worker failures.

## Exact implementation scope for approval

- `api/services/memory_service.py`: persist/reuse stable delivery ID for durable
  admission; independent initial publication and queued-on-publication-failure.
- `api/tasks/watchdog_tasks.py`: add a separate, narrowly named dispatch-recovery
  function/task without changing existing watchdog selection, grace or cadence.
- `api/celery_app.py`: register only the additional five-second recovery schedule.
- Focused local regression/diagnostic files and matching AGENTS/spec/report updates.

No schema migration needed; existing `celery_task_id` and job row suffice for
this design. No worker mutation code, settings flags, new endpoints, infrastructure,
SDK/MCP/UI or frozen acceptance criteria changes. The additional five-second
trigger and recovery-specific 3/4 s soft/hard task limits were expressly approved;
existing watchdog and extraction task limits remain unchanged.

## Implementation and bounded evidence checkpoint

Implemented independent initial dispatch, stable ID persisted at admission,
queued-on-publish-failure, SQL row/identity locks and same-ID recovery. Recovery
adds no reservations, jobs or mutation logic. One-second connection/socket/SQL
limits, 500 ms SQL lock limit and three-second cycle budget constrain recovery
operations; prefork soft/hard task enforcement was not demonstrated on Windows
solo. Preserve existing database connection options when adding query limits.
Legacy bound queued receipts with no delivery ID commit an ID first and publish
on a later cycle after fresh permission checks; new candidate admissions commit
their ID immediately. A skipped revoked/blocked identity is left queued, not
silently marked applied or terminal; deletion remains governed by existing policy.

Completed: 61 focused unit, 18 admission integration, 19 non-scheduled recovery
and one real local Beat/Redis/controlled-worker timing check. Scheduled result:
completed in **1.250 s**, one job/source/memory/revision/audit/outbox; real broker
duplicate ignored and mutation counts unchanged. The first timing artifact is
invalid because the harness's Beat constructor failed; it is preserved separately,
not treated as a candidate failure. Original 30.265 s failure artifact unchanged.
Evidence: `benchmarks/internal/reports/dispatch-recovery-repair-20261011.md`.

Owner approved commit and continuation to source repin after scoped diff/evidence
review. Runtime candidate freeze remains pending. No full P8, production,
worker-started crash recovery, acceptance dataset or pilot readiness claim.
