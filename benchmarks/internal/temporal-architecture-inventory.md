# Temporal-memory architecture inventory

Scope: tenant backend development baseline only. Holdout, prompt tuning and production changes are excluded.

## Current representation

- `Memory` stores ingestion/creation timestamps, last access, optional `expires_at`, archive state and predecessor ID. It has no explicit validity interval.
- `MemoryClaim` stores `observed_at` and one `effective_at`; `MemoryClaimRevision` stores observation time. Neither closes a validity interval with `effective_until`.
- `MemorySourceEvent` preserves source `observed_at` separately from receipt time and normalizes timestamps to UTC.
- Versions capture state changes and reasons but do not represent a queryable bitemporal timeline.

## Interpretation and state transitions

Extraction can describe temporal language and emits an expiry label, but the prompt explicitly assigns conversion of temporary language into a concrete expiration date to the application layer. No general effective-from/until interpreter exists. Conflict resolution has a narrow temporal-context heuristic that keeps differently timed statements together. Authority logic compares source priority and observed time; older equal-authority evidence is rejected. Active truth is primarily `is_archived` plus the activated claim revision.

## Lifecycle and background work

Lifecycle jobs decay importance from inactivity and archive low-value stale memories. Provenance retention redacts expired raw job payloads. These are storage lifecycle mechanisms, not semantic validity scheduling. No task activates future memories at effective-from or closes winners at effective-until.

## Qdrant and retrieval

Vector payloads include creation/access timestamps, archive state, predecessor, source event and provenance, but no validity interval. Qdrant filtering supports a `created_after` filter. API `time_filter_days` therefore means ingestion age, not “true at date X.” Retrieval has no `as_of`/historical mode and does not generally filter semantic expiration.

## Existing strengths and coverage

Tests cover UTC normalization, older equal-authority rejection, authority precedence, temporal KEEP_BOTH, predecessor/version linking, lifecycle decay idempotency, timezone-aware scheduling, retrieval age-filter plumbing, outbox behavior and PostgreSQL event deduplication.

## Principal architecture gaps probed by this baseline

1. No explicit memory/claim validity interval.
2. No scheduled future activation or interval closure.
3. No historical/as-of retrieval contract.
4. Creation-time filtering is not event-time or validity filtering.
5. Qdrant cannot enforce temporal validity because payloads lack it.
