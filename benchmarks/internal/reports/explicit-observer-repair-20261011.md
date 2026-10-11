# Approved observer-only repair — 2026-10-11

Backend HEAD `076e0edf4fc5b8f9c2f64b22498497fc55516cce`, unchanged. Existing
observer updated to **explicit-operations-observer-2**; owner subsequently approved
its commit and source/observer freeze. This is a format version,
not automatic interpretation V2 (still disabled). No private dataset/cases/labels
inspected or authored, provider calls, API/worker evaluation, backend/flag/rule
changes, AWS, deployment or publication. All test inputs synthetic unit fixtures,
not independently authored acceptance evidence.

## Scoped implementation

Existing `benchmarks/internal/explicit_operations_pilot.py` reused, no new framework:

Reviewable code/schema/test patch: `explicit-observer-repair-20261011.patch` in
this directory (six files; these artifacts were previously untracked, so Git's
patch shows complete files rather than a committed version-1 baseline).

- Existing read-only history/preference-review API paths and fresh-client steps.
  Explicit transient read/job polling only; never automatic POST retries.
- Owner-scoped, read-only repeatable-read SQL: memories/claims/revisions/audit,
  versions, jobs/receipt hashes, sources, outbox and guard lifecycle. 1000-row cap;
  no raw job payload/envelope, credentials or embeddings. Outbox ownership fallback
  supports deleted memory IDs. Queue observations remain operator evidence.
- True wall-clock deadline across HTTP phases/repetitions and SQL. P8 cases with
  fault injection require an operator removal-clock anchor, subtracting elapsed
  age from unchanged 30 seconds; initial preparation remains bounded. No fault hooks.
- Partial observations preserved on transport errors. Separate <=3 s diagnostic
  after-state cannot turn a late outcome into a pass. Failed safety assertions or
  uncertain writes stop later mutating steps/cases, retaining skipped counts.
- Closed <=16 KiB operator receipts bind run/case/source/kind and artifact hash;
  owner adjudication of fault removal/render/browser/queue evidence. Receipts must
  already be available; no shell, rendering or operator-wait framework. Claims/
  hashes remain operator attestations, not automatic artifact verification.
- Manifest pins observer and runtime/API/worker/application/dependency/settings
  evidence, supports validated local SQL schema. Pure preflight before consumption,
  execution recheck, no untracked api Python (including ignored), marker retained.
- Failed-read latency, coverage/missing/skipped/uncertain and rendering p95 retained.
  `pilot_qualified=false` and incomplete-latency flag remain: human reconciliation
  against unchanged frozen thresholds, no automatic ship/narrow/stop decision.

Public scenario/manifest schemas refreshed and operator schema added. Version-1
inputs rejected, not silently migrated. Compatible unused acceptance data remains
NOT ESTABLISHED; no request to author another dataset. These schema exports are
formats only, not cases, owner adjudication or acceptance.

## Focused verification

**26 mocked unit regressions PASS**, covering endpoints, absolute timeout, opt-in
polling/attempt retention, fresh-client lifecycle, after-state capture, safety and
unknown-write stopping, partial polls, receipt/clock binding, missing external data,
schema/runtime rejection, preflight/consumption, cross-case stopping, timeout read
latency and render aggregation. No API/provider traffic. One initial test-loader
attempt lacked sys.modules registration required by future Pydantic annotations;
corrected only fixture loading. Not an observer/backend failure.

**One actual PostgreSQL projection test PASS, 3.49 s.** Fresh random schema and
two synthetic owners/receipts; all eight SELECT projections executed read-only.
Only selected owner's receipt returned, binding/guard fields present, raw payload
absent. Initial fixture omitted required tenant plan and failed before projection;
corrected fixture, not backend or SQL semantics. Final source/schema targeted
checks also pass after requiring fault clock and including ignored Python source.

```powershell
.venv\Scripts\python.exe -B -m pytest tests/unit/test_explicit_operations_observer.py tests/integration/test_explicit_observer_sql.py -q -x --tb=short -p no:cacheprovider --basetemp=D:\memoryos\memory-api\.pytest_observer_20261011_d
# 26 unit PASS; initial integration fixture setup failure
.venv\Scripts\python.exe -B -m pytest tests/integration/test_explicit_observer_sql.py -q -x --tb=short -p no:cacheprovider
# 1 PASS in 3.49 s
```

Three public schema-export commands loaded successfully with no dataset argument
or target connection. Disposable cached PostgreSQL 16 image, --pull=never,
localhost 55471 and synthetic credentials; random schema removed. Only that test
container cleaned up; existing project containers unaffected.

Observer SHA-256 at this checkpoint:
`1820497f88b1758279b299f40283f58e738f23985011e43e51c71ebb2ba0c87f`.
Public schema SHA-256:
scenario `bc7b97e89d4554379395110f5020f5faa68650cd34324e4ab279e056af536c6e`;
manifest `3156198969d582571aa9887b42920bbaf7b29b0af5af71fb2a7618ac7c346520`;
operator `c5d912d3649045ca884614506d4112e0a7d5fea4ab7b63859fd95bf2187096a4`.
Final schema/recovery-clock targeted checks: 3 PASS in 0.70 s. No threshold change.

## Scope and remaining gates

Changed files: existing observer; scenario/manifest/new operator schema exports;
`tests/unit/test_explicit_operations_observer.py`,
`tests/integration/test_explicit_observer_sql.py`; matching docs/roadmap/report.
Other uncommitted local-slice glue/evidence and unrelated work preserved.

This tests mechanics, not the frozen 30/60-case acceptance contracts or backend
readiness. Operator must verify running artifacts/effective settings and provide
properly coordinated fault/render/browser/queue receipts. Observer cannot create
fixtures, inject faults, render UI or adjudicate usefulness. No SDK, production,
replacement/choice, full P5 label consumption or full P8 worker-crash claim.
The previous local runtime was removed; no frozen acceptance target exists here.
Safety uncertainty alone is not a reproduced violation authorizing patch-and-rerun.

Owner approved commit and source/observer freeze after scoped diff/test review.
Actual runtime verification and independent-data readiness remain pending.
No independent evaluation, new dataset, backend patch or wider work authorized.
