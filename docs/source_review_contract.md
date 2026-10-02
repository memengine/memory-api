# Non-activating source reviews

Release status: implemented locally; not yet deployed or published in either SDK.
This is a review-delivery fallback, not a new semantic classifier or an extraction
accuracy certificate. Existing two-memory clarification contracts are unchanged.

## Boundary

A verified user quote establishes attribution, not a committed, correctly scoped
property value. When the existing source decision recognizes uncertainty or
ambiguity but cannot produce a usable option, it may attach a backend-only
`restate_source` outcome. The worker persists that outcome with the existing
pending candidate. No fake Memory, embedding, active claim or selectable model
value is created for this fallback.

Only recognized semantic outcomes qualify. Invalid source roles/hashes, stale
targets, malformed provider responses, unavailable providers and incomplete
context remain processing failures, not questions attributed to the user.
The model still controls semantic interpretation; misclassified uncertainty and
incorrect interpretations that pass the existing option checks remain separate
limitations. This change does not fix or weaken those semantic checks.

## Retrieval contract

Current `POST /v1/memories/retrieve` responses add `source_reviews`, defaulting to
`[]`. Historical `as_of` requests do not surface current reviews. A review contains:

- `id`, opaque `version`, `kind: "restate_source"`, `question`, `expires_at`;
- optional `target_memory_id` and `current_memory_content` (a preview capped at
  1,000 characters, with an ellipsis when truncated—not a user-confirmed claim);
- `actions`: `restate`, `dismiss`, and, for a still-valid target, `keep_current`.

Responses expose at most three recent candidates and hydrate targets in one
additional bounded query. Reviews remain pending across reads; merely seeing a
question does not consume it. Foreign, expired and changed targets are excluded.
TTL is seven days after the server's last observation. No processing metadata,
raw source transcript, model reasoning or new authority appears in this view.

The response is a bounded recent view, not a complete review inbox. Stale rows
are conditionally expired without overwriting concurrent worker refreshes, so
subsequent reads advance past them. The service inspects at most three selected candidate rows per call;
older reviews may need a subsequent read and are not paginated in this slice.
Database scan cost and loaded p99 have not been measured. Do not
advertise guaranteed delivery of every pending review or a load-tested SLA.

Existing retrieval context remains the last admitted memory state. This slice
does not add a global pending-property suppression system or promise immediate
read-your-writes while asynchronous ingestion is still queued.

## Answer contract

`POST /v1/memories/source-reviews/{id}/answer` takes:

```json
{
  "external_user_id": "customer-owned-user-id",
  "version": "<opaque version returned with the review>",
  "action": "restate"
}
```

The normal authenticated tenant/API-key write policy applies. The backend looks
up an existing customer identity, locks the owned pending record and target,
and rechecks the source version, target content/authority/scope/temporal validity
and expiry. Unknown/foreign reviews return 404; stale or closed reviews return
409; unavailable actions return 422. No new customer identity is created here.

- `keep_current`: dismiss the interpretation while preserving the stored target
  unchanged. It does not promote authority, reactivate or create a memory.
- `dismiss`: dismiss the interpretation without changing any memory.
- `restate`: return `resolved: false`, `next_step: "add_memory"`. The review stays
  pending. Ask the user in the customer's existing chat, then submit their actual
  new statement through normal `add` ingestion. Do not submit a model paraphrase
  as direct user input, or treat this answer as completed storage.

The existing job lifecycle reports what ingestion actually did. If it remains
pending, do not report successful resolution. A changed target invalidates the
old review; source-review resolution is not yet linked atomically to a new job.

## SDK contract and release boundary

Python sync/async clients expose `result.source_reviews` and
`answer_source_review(id, external_user_id=..., version=..., action=...)`.
TypeScript exposes `result.sourceReviews` (absent on older/frozen clients;
default `[]` on the normal client) and `answerSourceReview({...})`.
Existing clarification fields and `answer_clarification`/`answerClarification`
remain compatible. No assistant-specific phrase matching or UI was added.

Model calls, holdouts, MCP deployment and production configuration are unchanged.
Publish versioned SDK releases and update public docs only after the backend
deployment is verified; do not claim these new methods exist in older releases.

## Verification

Unit tests exercise semantic-fallback eligibility, non-activation, ownership,
write permission, expiry, target and source-version changes, repeat delivery,
bounded preview, and target-aware pending-candidate deduplication.
Real PostgreSQL tests exercise the existing parser/worker, persisted pending
record, review projection and locked answer. Existing canonical clarification
selection tests verify later retrieval returns its winner.
Model outputs and embeddings in these lifecycle tests are controlled. They are
not fresh real-model evaluations, independent holdouts, or load tests.
