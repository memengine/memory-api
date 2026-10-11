# Explicit-operation observer: implementation, not acceptance evidence

The user approved a thin evaluator and executable input validation on 2026-10-09.
This implementation does not change backend behavior, enable flags, provision
services, author scenarios, execute acceptance data or recreate AWS resources.
The approved promise is committed-state typed preferences, one declaration per
request. Queue acceptance is not application; pre-commit reads may show the old
committed value. V2 and automatic interpretation remain disabled.

## Authoritative executable interface

`benchmarks/internal/explicit_operations_pilot.py` defines `Scenario` (one JSONL
line) and `LocalManifest` (one JSON object). Schema version is
`explicit-operations-observer-2`. Version 2 is the approved observer-only repair;
version-1 inputs are rejected, not silently converted. Dataset compatibility
remains NOT ESTABLISHED. This interface is not a
claim that any previously authored dataset already conforms to it.

Export schemas without reading a dataset or connecting to services:

```powershell
.venv\Scripts\python.exe -B -m benchmarks.internal.explicit_operations_pilot --schema scenario
.venv\Scripts\python.exe -B -m benchmarks.internal.explicit_operations_pilot --schema manifest
.venv\Scripts\python.exe -B -m benchmarks.internal.explicit_operations_pilot --schema operator
```

The independent owner, not the implementation assistant, can validate an existing
private dataset using the following command. Replace PRIVATE_PATH with its actual
path. Validation prints the raw-file SHA-256 and coverage counts only; it does
not write evidence or connect to API/database/provider services.

```powershell
.venv\Scripts\python.exe -B -m benchmarks.internal.explicit_operations_pilot --dataset PRIVATE_PATH
```

Successful validation means structural validity only. It does not certify human
adjudication, independence, unused status, coverage sufficiency or acceptance.
Human-adjudication fields are owner attestations. Cases/labels remain private.
There is no instruction here to recreate data; check any owner-held existing
dataset against the exported interface first.

## Scenario fields and observations

A scenario records requirement, coverage group, language, human adjudication,
synthetic SQL owner UUIDs, ordered observation steps and external evidence needs.
No examples/cases/labels were authored by the implementation assistant.

Steps are restricted to existing add, explicit read, memory lookup/history,
owned preference-review listing, job lookup/polling, SQL and closed operator
evidence. There are no shell/fault/render hooks. Valid write/read payloads use the
existing authoritative backend request validators. Every add has exactly one
preference declaration. Every read is preference-only with explicit context;
ordinary semantic retrieval cannot be used as a replacement.

Negative-input controls must fail backend request validation and expect HTTP
refusal. A schema-valid request cannot masquerade as an invalid-input control
to enter a semantic path. Authentication controls can use ApiKey (default) or
Bearer, with credentials taken only from named environment variables. Request
headers/credentials are not written to evidence. Replay steps support one to
five attempts, sequential or concurrent. Changed-payload replay uses distinct
steps with the same key. No automatic request retry or output repair is used.

Dynamic values use the closed `Reference` object with a `reference` field:
step name, then `#`, then JSON Pointer. Requests may reference earlier observations;
checks may reference current/earlier ones. Observations contain HTTP status,
response body and request elapsed time; repeated attempts contain an `attempts`
array; polling preserves completed responses even if a later transport fails.
`fresh_client=true` uses/closes a separate HTTP client, not a browser/SDK promise.
Only read/poll_job may opt into fixed 429/502/503/504 responses using
`retryable_http_statuses`; each response is retained and shares the deadline.
Add never auto-retries; one-to-five attempts remain explicitly declared.
No arbitrary URL, code or SQL is allowed.

Independent checks use JSON Pointers with equals, count, contains or absent.
The owner explicitly identifies safety checks. A failed safety check stops
later cases; those cases are retained as not executed, never silently dropped.
This is an evaluation stop, not proof that a safety violation was reproduced:
the reviewer must establish input, before/after state and violated invariant.

Write scenarios require SQL observations before and after their writes. Fixed,
parameterized SQL projections inspect memories, claims, revisions, audit,
versions, jobs/receipt bindings, source events and vector outbox
rows for frozen synthetic owners only, in read-only repeatable-read transactions.
Results above the per-table 1,000-row bound fail rather than truncate evidence.
Job projection contains guard/visibility lifecycle fields, excluding raw
payloads/envelopes, credentials and embeddings. Outbox owner projection can retain
visibility after memory deletion. Queue counts remain external operator evidence.
The observer does not create fixtures or run migrations. It cannot independently
establish that a manifest's declared owners are synthetic; the owner must verify.

## Meaning of checks

The observer does not infer applied/deduplicated/rejected semantics from status.
An independent expectation must correlate job-created ID, fetched declaration,
predecessor/provenance and scoped selected winner to claim applied/current.
Matching a pre-existing value is only observed-current evidence. SQL checks must
assert winner, retirement, logical mutation counts, authority and unrelated-state
preservation. Separate observations are not an atomic lifetime promise; concurrent
successors must be explicitly adjudicated by the frozen scenario.

The initial scenario deadline is a true 30-second wall-clock bound across HTTP
phases/repetitions and SQL. P8 fault-injection cases must name recovery_clock_step:
an external fault-removal receipt anchors the existing 30-second recovery deadline,
deducting time already elapsed, never restarting it at receipt-read time. Initial
preparatory steps remain bounded by the original 30 seconds.
Failures retain partial observations and a separate <=3-second read-only diagnostic
after-state; late state never salvages an expired outcome. Safety failures stop
later writes/cases; uncertain post-write transport/after-state failures stop later
cases. Failed-read duration is retained. Read p95 is diagnostic only:
independent UI timing/coverage and operator adjudication still determine complete
acceptance evidence. The summary always states `pilot_qualified=false` and
`latency_is_complete_acceptance_evidence=false`; do not present it as a contract pass.

## Execution gate (not authorized or performed)

Execution requires an owner-held local manifest attesting isolated synthetic
fixtures, effective explicit-preference enablement, interpretation/V2 disabled,
controlled embeddings and blocked external-provider network access. These are
owner attestations, not configuration changes or live verification by this tool.
Manifest v2 requires run_id, exact observer_sha256, source/runtime HEAD agreement
and operator-verified API/worker/application/dependency/effective-settings hashes.
These attestations are not live verification by the tool. database_schema strictly
selects a local schema, default public. API/database origins remain loopback;
redirects/proxies/transport retries disabled. Only declared safe polling retries
are allowed. HEAD/tracked api source must agree; untracked api Python, including
ignored files, is refused. A Git HEAD check does not attest a running build.

Only after separate approval, the owner supplies `--execute`, matching
`--approved-dataset-sha256` and `--approved-manifest-sha256`, `--manifest`,
`--dataset` and a fresh private `--output` directory. Database URL and principal
credentials are environment variables, never manifest secrets. Execution mutates
synthetic state through the API, but its direct SQL connections remain read-only.

A dataset-side exclusive `.consumed` marker is written before execution; an
existing marker or output directory refuses a normal rerun. Failures after the
marker is written do not permit an automatic restart. Copying/moving files is not
permission to evade consumption. Private evidence is written incrementally, with
a final aggregate summary and dataset/manifest freeze hashes. Pure local/source/
environment preflight occurs before consumption and is rechecked at execution.
It does not contact/prove target readiness. Later failures may consume the run
even without a request; review manually, never automatically restart.

## Operator evidence (not acceptance examples)

An external step declares external_kind and external_env (local regular JSON
file path) matching external_requirements. Export --schema operator for exact
fields. It binds run_id/case_id/source_head/kind; requires artifact_sha256,
owner_adjudicated=true and timezone-aware recorded_at. Closed, maximum 16 KiB.
Fault records need fault_removed_at; rendering records need measured
first_rendered_elapsed_ms; browser records need browser_identity_checked; queue
records need queue_depth/reserved_job_count. Receipt must exist when the step
runs: no operator wait, fault injection, rendering or fixture creation by observer.
The independent operator coordinates and adjudicates artifacts; claimed hashes
are not automatic verification of their contents. Receipt bytes are hashed into
private evidence. Missing/mismatched evidence fails. Summary retains coverage,
skipped/uncertain/missing-external counts and operator first-render p95. Useful-read
and ship/narrow/stop adjudication remain human under unchanged frozen thresholds.

## Explicit limitations

- Fault injection and fixture preparation remain with the independent operator;
  no shell hooks or new fault framework were introduced.
- Browser identity substitution and customer rendering require independent
  external evidence. Declared external requirements remain visible in summary.
- No acceptance threshold engine automatically ships/narrows/stops the product.
  The owner reconciles all results, missing coverage and external evidence with
  the approved frozen contract. Aggregate counts must not hide skipped cases.
- Replay refusal is checked, not repaired. Admission and pre-worker dispatch
  repairs have supporting regression evidence at 076e0ed, not independent P8
  qualification or permission for further backend changes.
- Controlled local providers do not establish cloud deployment, real-provider
  reliability, production latency/load, released SDK or customer readiness.

Observer version 2 verification: 26 mocked unit regressions and one actual
disposable PostgreSQL projection check passed; three schema exports loaded.
Evidence: benchmarks/internal/reports/explicit-observer-repair-20261011.md.
No private-data validation, acceptance evaluation or provider calls; backend and
unrelated edits preserved. Disposable SQL test container removed. Owner approved
observer commit/source freeze; verified runtime and unused independent data precede separately approved
acceptance. Do not ask for another dataset at this checkpoint.
