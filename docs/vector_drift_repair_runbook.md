**Purpose**
This runbook explains the one-time drift repair tool for PostgreSQL and Qdrant, why it was added, and how to use it safely later.

**Why This Exists**
Daily reconciliation in [reconciliation_tasks.py](d:/memoryos/memory-api/api/tasks/reconciliation_tasks.py) intentionally refuses to auto-repair when `missing_in_qdrant > 100`. That is the correct production safety rule, because a large mismatch should not be silently repaired during normal operation.

Your live database currently has historical drift, so normal reconciliation can detect it but will not restore all missing vectors by itself.

**What I Added**
I added:
- [repair_vector_drift.py](d:/memoryos/memory-api/scripts/repair_vector_drift.py)
- this runbook: [vector_drift_repair_runbook.md](d:/memoryos/memory-api/docs/vector_drift_repair_runbook.md)

The script is a one-time operator tool. It does not replace the normal daily reconciliation job.

**What Problem It Solves**
It helps repair two kinds of drift:
- PostgreSQL memory exists, but the Qdrant vector is missing
- Qdrant vector exists, but there is no active PostgreSQL memory row

**Important Safety Model**
- PostgreSQL remains the source of truth
- the script does not weaken the normal `missing_in_qdrant > 100` protection
- missing vectors are repaired by enqueueing into `vector_sync_outbox`
- outbox processing then writes them back into Qdrant
- orphan vectors can be deleted directly from Qdrant

**High-Level Flow**
1. Scan active PostgreSQL memories.
2. Check which of those are missing in Qdrant.
3. Scan Qdrant vectors.
4. Check which of those are orphaned.
5. Optionally enqueue missing PostgreSQL memories into `vector_sync_outbox`.
6. Optionally process outbox rows until empty.
7. Optionally delete orphan vectors.

**First Command To Run**
Always start with:

```powershell
osenv\Scripts\python scripts\repair_vector_drift.py --dry-run
```

This is safe and read-only.

**What Each Flag Means**
- `--dry-run`
  - read-only mode
  - scans PostgreSQL and Qdrant
  - prints drift counts
  - makes no changes
- `--repair-missing`
  - finds memories present in PostgreSQL but missing in Qdrant
  - enqueues `upsert` rows into `vector_sync_outbox`
- `--delete-orphans`
  - deletes Qdrant vectors that do not have an active PostgreSQL memory row
- `--process-outbox`
  - runs the outbox processor in a loop until no more rows are claimable
  - useful right after `--repair-missing`
- `--max-missing-repairs 500`
  - limits how many missing memories are enqueued in one run
  - useful for staged repair
- `--page-size 200`
  - controls scan batch size for PostgreSQL and Qdrant
  - default is usually fine

**Command Examples**
Inspect only:

```powershell
osenv\Scripts\python scripts\repair_vector_drift.py --dry-run
```

Meaning:
- tells you current drift
- safest command
- use this first every time

Delete only orphan vectors:

```powershell
osenv\Scripts\python scripts\repair_vector_drift.py --delete-orphans
```

Meaning:
- removes vectors that should not exist
- does not restore missing vectors

Repair 500 missing vectors and sync them immediately:

```powershell
osenv\Scripts\python scripts\repair_vector_drift.py --repair-missing --process-outbox --max-missing-repairs 500
```

Meaning:
- finds up to 500 PostgreSQL memories missing in Qdrant
- adds repair rows into `vector_sync_outbox`
- immediately processes the outbox
- safest real repair command to start with

Full repair pass:

```powershell
osenv\Scripts\python scripts\repair_vector_drift.py --repair-missing --delete-orphans --process-outbox
```

Meaning:
- tries to clean both missing vectors and orphan vectors in one run
- best used only after smaller test batches are stable

**How To Read The Output**
Example:

```python
{
  'missing_in_qdrant': 10006,
  'orphan_in_qdrant': 0,
  'enqueued_missing_repairs': 0,
  'deleted_orphans': 0,
  'outbox_cycles': 0,
  'outbox_done': 0,
  'outbox_failed': 0
}
```

Meaning:
- `missing_in_qdrant`
  - active PostgreSQL memories that do not currently have vectors in Qdrant
- `orphan_in_qdrant`
  - vectors in Qdrant with no active PostgreSQL memory row
- `enqueued_missing_repairs`
  - number of new repair rows added to `vector_sync_outbox`
- `deleted_orphans`
  - number of orphan vectors removed from Qdrant
- `outbox_cycles`
  - how many times the script ran the outbox processor loop
- `outbox_done`
  - how many outbox rows were successfully applied
- `outbox_failed`
  - how many outbox rows failed permanently during this run

**Recommended Repair Strategy**
1. Run `--dry-run`
2. If orphan vectors exist, run `--delete-orphans`
3. Run a small repair batch:

```powershell
osenv\Scripts\python scripts\repair_vector_drift.py --repair-missing --process-outbox --max-missing-repairs 500
```

4. Run `--dry-run` again
5. Repeat until `missing_in_qdrant` is low or zero

**Why I Did Not Change Daily Reconciliation Instead**
The daily reconciliation guard is still correct:
- if drift is very large, it should alert instead of silently doing a huge repair
- large repair is an operator task, not a background task

So instead of weakening the production rule, I added a separate repair tool.

**What This Script Does Not Change**
- it does not change the normal live write path
- it does not disable the daily reconciliation safety threshold
- it does not delete processed outbox rows
- it does not make PostgreSQL any less authoritative

**Verification After Repair**
Run:

```powershell
osenv\Scripts\python scripts\repair_vector_drift.py --dry-run
```

Then confirm:
- `missing_in_qdrant` is near zero
- `orphan_in_qdrant` is near zero
- future missing-vector reconciliation can work again on normal-sized drift

**If Repair Fails**
- check `docker compose logs celery-worker --tail=200`
- check `docker compose logs api --tail=200`
- check `vector_sync_outbox` rows with `status='failed'`
- retry in smaller batches with `--max-missing-repairs`

**Short Version**
- `--dry-run` = inspect only
- `--repair-missing` = enqueue missing vectors for repair
- `--process-outbox` = actually apply those repairs
- `--delete-orphans` = remove bad vectors from Qdrant
- start small, verify, then repeat
