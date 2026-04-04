# Importance Scorer Verification

This document records the current verification status for the Importance Scorer checklist.

## Result

- Overall status: PASS
- Test command: `osenv\Scripts\python -m pytest tests/unit/test_importance_scorer.py`
- Latest test result: `9 passed`

## Files Verified

- Scorer: `api/services/importance_scorer.py`
- Decay task: `api/tasks/decay_tasks.py`
- Celery app: `api/celery_app.py`
- Tests: `tests/unit/test_importance_scorer.py`

## Checklist

### 1. Goal memories score higher than fact memories for equivalent content

- Verified with identical raw importance inputs:
  - goal category receives `+1.5`
  - fact category receives `+0.0`
- Outcome: `PASS`

### 2. Decay task archives stale low-importance memory

- Verification case:
  - `last_accessed_at = 40 days ago`
  - `importance_score = 2.0`
- Ran `run_decay_cycle(...)`
- Confirmed:
  - `is_archived = True`
  - archive count = `1`
  - `AuditLog(action=archived)` created
- Outcome: `PASS`

### 3. Access boost caps at +0.5 over 100 accesses

- Verification case:
  - start memory importance = `5.0`
  - call `increment_access()` 100 times on the same memory
- Confirmed:
  - `access_count = 100`
  - final importance = `5.5`
  - total boost = `+0.5`
- Outcome: `PASS`

### 4. Celery beat task appears in schedule

- Worker runtime check:
  - command: `docker compose exec worker celery -A api.celery_app.celery_app inspect scheduled`
  - output: `1 node online`, `- empty -`
- Important note:
  - `inspect scheduled` shows worker ETA/countdown tasks currently reserved
  - it does **not** list the static Celery beat schedule unless a task has already been sent to a worker
- Direct beat schedule verification in the running beat container:
  - command:
    - `docker compose exec beat python -c "from api.celery_app import celery_app; print(celery_app.conf.beat_schedule)"`
  - output contains:
    - `archive-stale-low-importance-memories`
    - `api.tasks.decay_tasks.archive_stale_low_importance_memories`
    - `crontab: 0 2 * * *`
- Beat container log also shows:
  - `beat: Starting...`
- Outcome: `PASS`

## Runtime State

- `docker compose ps` confirmed these services were running during verification:
  - `worker`
  - `beat`
  - `redis`
  - `postgres`

## Notes

- Added `increment_access()` as a public alias to the scorer so the API matches the verification checklist wording.
- The scorer rounds cumulative access-boost updates to keep the final `+0.5` cap numerically stable after 100 increments.
