# Approved pre-worker dispatch recovery repair — 2026-10-11

Base HEAD: `4bf8884737ef58416bb34b9d831b0836333d21c0`. Repair was uncommitted
during verification; the owner subsequently authorized commit and source repin.
This is focused regression evidence,
not independent pilot acceptance. No private inputs/labels or provider calls.

## Diff scope

| File | Necessary change |
| --- | --- |
| `api/services/memory_service.py` | Persist stable delivery ID with durable admission, publish independently of cache writes, retain queued receipt/reservation on publication failure, publish same ID with retries disabled. |
| `api/tasks/watchdog_tasks.py` | Separate durable dispatch-recovery task; only queued bound jobs that have not started. SQL skip-locked row/identity checks and same-ID publication, bounded recovery connections/queries/publish. Existing watchdog function/grace/cadence unchanged. |
| `api/celery_app.py` | Register the approved 5 s schedule and recovery-task-only 3/4 s soft/hard limits. Existing extraction limits unchanged. |
| `tests/integration/test_durable_add_replay.py` | Existing duplicate-worker test uses the newly persisted stable delivery ID. |
| `tests/integration/test_durable_dispatch_recovery.py`, `dispatch_recovery_local_runtime.py` | Focused recovery cases, actual API/publisher child exits, isolated real Beat/broker/controlled worker. |
| `tests/unit/test_durable_dispatch_transport.py` | Actual async dispatch transport identity and unchanged watchdog/extraction timing invariants. |
| AGENTS/spec/candidate metadata and this report | Evidence, preserved failures, scope boundaries and review checkpoint. |

Code/test diff: `dispatch-recovery-repair-20261011.patch` in this directory.
No migration, new table, worker mutation changes, deletion policy, endpoint,
flags, infrastructure, SDK/MCP/UI or acceptance-threshold changes.

## Verification

99 distinct focused checks passed across recorded runs:

- 59 unit checks: `test_memory_service.py`, `test_memory_add_replay_binding.py`,
  `test_explicit_preference_contract.py`; exit 0, 14.50 s.
- Two additional transport/schedule invariants: exit 0, 2.97 s; verifies actual
  `_send_extraction_task` passes stable ID and no retry, preserves legacy options,
  and preserves watchdog/extraction timing alongside the approved new trigger.
- 18 existing real SQL/Redis admission checks passed in the first combined run;
  that run then failed in a new test's incorrect reservation-key call (test-only),
  corrected before recovery verification. No old acceptance/holdout data used.
- 19 non-scheduled recovery checks: exit 0, 43.46 s (command below). Covers cache
  failure without gating dispatch, queued receipt/reservation on publish failure,
  lost acknowledgement, four concurrent recovery calls against a paused actual
  admission/publication, legacy ID assignment, default bounded SQL factory with
  schema-option preservation, processing/terminal exclusion, owner/tenant/receipt
  deletion, key revocation/read-only and blocked/inactive identity, actual API
  exit after commit, publisher exit before and after real broker send.
- One scheduled timing check: exit 0, 20.37 s including process startup and
  duplicate delivery. **1.250 s fault-removal-to-terminal observation**, one
  job/source/memory/revision/audit/outbox; same-ID duplicate received by real worker
  returned `duplicate_ignored` and all SQL counts stayed unchanged.

```powershell
.venv\Scripts\python.exe -B -m pytest tests/unit/test_memory_service.py tests/unit/test_memory_add_replay_binding.py tests/unit/test_explicit_preference_contract.py -q -x --tb=short -p no:cacheprovider
.venv\Scripts\python.exe -B -m pytest tests/unit/test_durable_dispatch_transport.py -q -x --tb=short -p no:cacheprovider
.venv\Scripts\python.exe -B -m pytest tests/integration/test_durable_dispatch_recovery.py -k 'not real_scheduled' -q -x --tb=short -p no:cacheprovider -p no:asyncio
.venv\Scripts\python.exe -B -m pytest tests/integration/test_durable_dispatch_recovery.py -k real_scheduled -q -x --tb=short -p no:cacheprovider -p no:asyncio --basetemp=D:\memoryos\memory-api\.pytest_dispatch_recovery_20261011_b
```

The completed timing artifact is guarded against accidental rerun/overwrite.
These commands document actual runs, not approval for another timing experiment.

Scheduled evidence: `dispatch-recovery-repair-20261011-scheduled-v2.json`, SHA-256
`10c6a5a3de59d398c81415bbff298c563d2a439743c98eb20e61ad89b36967cd`.
The `v2` suffix distinguishes harness attempts, not interpretation V2.
Worker log: recovery published the receipt in 0.562 s, extraction completed in
0.204 s; later broker delivery of the same ID was ignored. Source-level worker
claim/mutation implementation was reused unchanged.

## Preserved evidence and invalid harness attempt

Original reproduced pre-repair failure:
`dispatch-recovery-diagnostic-20261011.json`, SHA-256
`9c1b8f79fceb50cda213af5d2c4517e0db4ecb36d4e8a5416e55b43f03e81e91`.
Untouched: queued at 30.265 s, no dispatch/mutation.

First repaired scheduled attempt: `dispatch-recovery-repair-20261011-scheduled.json`,
SHA-256 `0617161fe49a8fd133b1a41eecae2f466475d13b033c8a1470fcc2ba3fa50a60`.
Its raw result says FAIL/queued, but the attempt is **INVALID**: Beat exited on
`Service.__init__() got an unexpected keyword argument 'scheduler'`. No scheduler
ran. The artifact is preserved and not used to infer backend failure or recovery.
Corrected only harness constructor (`scheduler_cls`) and readiness/process-liveness
checks before the valid timing attempt; no backend tuning against timing results.
The system pytest temp directory was inaccessible, so scheduled runs used verified
fresh workspace-local directories. Neither issue was an acceptance failure.

## Runtime isolation and limits

Cached PostgreSQL 16/Redis 7 images, `--pull=never`, dedicated localhost ports
55471/55472 and synthetic credentials. Per-recovery-test random SQL schema;
fixed test DSNs only. Celery application uses actual production app factory plus
test-process overrides for the disposable broker and the single approved recovery
schedule; all unrelated scheduled jobs are excluded. Worker is Windows solo;
actual task, SQL storage and broker with controlled declaration pipeline/embedding,
no model/vector/provider calls. Test-process explicit settings copies only;
interpretation V2 and real settings unchanged. Only disposable containers/child
processes were cleaned up; existing project containers remain untouched. No AWS.

Default recovery SQL builder now has 1 s connection/statement and 500 ms lock
timeouts while preserving preexisting URL options. Publish socket/connect limits
are 1 s, retries disabled; cycle budget 3 s, task soft/hard limits 3/4 s. Solo
does not establish prefork time-limit enforcement. The measured scheduled check
used injected isolated SQL factory; a separate integration check exercised the
actual default factory with those bounded connection options. That option-
preservation improvement followed timing and does not alter the injected-factory
scheduled path, so no repeated timing run was needed.

No general 30 s guarantee from this one sample: scheduler availability, database
and broker outages, workload/queue delay and deployed behavior remain unqualified.
Recovery stops at worker start; 10-minute worker stale timeout and extraction
105/120 s limits remain unchanged. Crashes between mutation commit and job
completion remain outside this repair. Full P8 and pilot readiness are not earned.
Revoked/blocked identities are skipped and left queued; no new terminal/deletion
policy is introduced. Legacy bound jobs with no delivery ID require an additional
cycle to persist identity before publication. Existing deletion ends protection.

Final code SHA-256 (working files):

```text
api/services/memory_service.py 3185d0ffb9342e1b0fc3b21324e23b9cbff96ef36fa2cd0c47fe7143946f46b9
api/tasks/watchdog_tasks.py f143136dfc313d3fefe043a4c3a163e7ec7a0ecc02e9c4e3583a710f376d42d2
api/celery_app.py f53ea8aff7e69ee3c1fa66c28d3fa2e34bfd187ab68f687f4d131ed153332493
```

Scoped diff checks passed. Owner authorized commit and source repin after review.
No additional fault investigation, dataset, acceptance run,
deployment or publication authorized.
