# Selective local fault controls — 2026-10-12

Approved implementation/checks only. No live attachment, fault injection, memory
journey, independent acceptance cases, evaluation or backend repair authorized.
Module: `benchmarks/internal/explicit_fault_operator.py`; synthetic mechanism
tests: `tests/unit/test_explicit_fault_operator.py`. Importing has no side effect.
No CLI, web control endpoint, arbitrary code execution or new service introduced.

## Boundaries and future attachment

An independently frozen Selector binds run/case/source, synthetic tenant/owner
UUIDs and request-key SHA-256. Never place credentials or raw payload in controls.
The selector identifies a persisted job by a fixed parameterized SQL projection;
job/task/queue/advertised-tenant identity must match before dispatch refusal.
Unknown or other SQL-bound identities pass through unchanged under valid controls.
This deliberately refuses publication before calling the real publisher; it is
not a physical Redis/network-outage simulator.

`Gate` requires an explicitly verified isolated runtime, a replay_<32 hex> schema
and localhost Redis 55472/0. `sql_lookup` only accepts replay_test on localhost
55471, establishes read-only transaction/schema and one-second statement timeout.
Queue collection only accepts that isolated Redis and known extraction queues.
These checks supplement owner/runtime verification; they do not establish synthetic
ownership or attest the running process/source by themselves.

For a separately approved bootstrap:

1. Create Controller with start_armed=False in a fresh private control directory.
   Attach API and worker Gate instances using the fixed SQL lookup and the same
   frozen selector/control. Startup does not spend the fault's duration.
2. API factory wraps the existing celery_app.send_task, affecting only the selected
   extraction publication. Worker factory wraps _publish_durable_reference. The
   status-read factory wraps the existing async MemoryService.get_job_status.
   Existing authentication, transactions, source/authority rules, scheduler,
   worker claim/mutation safeguards and watchdog limits remain intact. These are
   wrapper factories, not a live installer; nothing was attached in this step.
3. Arm only after readiness. Duration is at most 10 seconds (default 5). No
   rearming/reuse is accepted. Explicit release records UTC and same-host monotonic
   removal time. Relevant gates acknowledge the released control.
4. Collect actual dispatch/read recovery and mutation evidence separately. The
   30-second frozen recovery clock starts at removal, not at acknowledgement,
   receipt reading or a later Northstar click. Initial preparation deadline and
   original safety/stop criteria remain unchanged.

API status refusal is 503; Northstar's current error path does not automatically
resume polling. An explicit recheck after restoration is required. Do not advertise
automatic UI recovery or treat a status-only fault as scoped-retrieval DB coverage.

## Bounded restoration and receipts

Controller automatically removes injection at expiry and marks the trial invalid.
Gates also cease injection at their pinned expiry if the controller/collection
fails. Automatic expiry is diagnostic-only, never a qualifying recovery result.
Before expiry, invalid/missing/rebound control state stops the experiment rather
than silently repairing it. Do not ignore these failures or restart a consumed run.
Evidence collection failure cannot keep a released/expired injected fault active;
missing collection prevents valid removal acknowledgements/receipt export.

Removal receipt export requires operator release, valid state, selected refusal
observations and API+worker acknowledgements for dispatch (API for status read).
It creates a new hashed artifact and observer-compatible unadjudicated receipt.
A role acknowledgement proves control observation, not actual process identity,
successful publication, network health, committed mutation or human adjudication.
Those must be established by the independently reviewed runtime/recovery evidence.
No extra fields or safety threshold engine were added to observer v3.

Queue snapshots use an atomic read-only Redis batch: tenant reservation counter,
global extraction-queue ZCARD and selected tenant/job membership. Missing counter
is an observed absent key (zero), while transport/parsing/accounting errors remain
errors. Counts do not replace SQL logical-mutation evidence. `queue_receipt` hashes
actual snapshot bytes, refuses overwrite and leaves owner_adjudicated=false.

## Demonstrated and unresolved

21 synthetic checks passed with persisted temporary controls/receipts and fake
dispatch, status, SQL and Redis callables. No real API/worker/Redis/SQL contacted.
Checks cover selectivity, exact delivery binding, 503/passthrough, prepared startup,
bounded invalid expiry, corrupt state, missing role acknowledgement, collection
failure, isolated-target constraints, read-only projection and receipt hashes.
This is tooling regression evidence, not recovery, one-mutation or acceptance proof.

Still unresolved: approved runtime attachment/role attestation; actual scheduled
refusal/restoration; source/application/bootstrap/control hash reconciliation;
operator coordination within deadlines; physical broker outages; worker-started
crashes; scoped-retrieval database faults; independent unused data and complete P8.
The old runtime/manifest remains historical and is not silently refrozen.

Evidence: `benchmarks/internal/reports/explicit-fault-operator-20261012.md`.
Owner approved the scoped tooling commit. Next action: a bounded live control
diagnostic proposal. Separate approval precedes
any attachment or live mechanism diagnostic; no acceptance data needed for it.
