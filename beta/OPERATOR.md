# Explicit service-correction review (local beta)

This API lets an authorized operator explicitly bind a retained service correction
to its current target. It does not infer a property, target, value, scope or
authorization from chat. It is separate from verified-user restatement reviews
and typed preferences. There is no dashboard button or SDK adapter in this change.

Every route requires a **currently active tenant-admin API key**, rechecked in SQL
on each request. Ordinary read/write keys and bearer/Clerk sessions are not
supported by this bounded operator interface. Tenant and owner must remain active;
blocked owners are hidden. Responses use the normal request-ID/timestamp envelope.

## Discover, preview, decide

1. `GET /v1/memories/service-corrections?external_user_id=OWNER&limit=20`
   returns retained pending correction IDs/content/category/created_at for that
   owner. Maximum limit 50; continue with the returned `next_cursor` as `after`.
   No target is automatically selected.
2. The operator explicitly chooses a current memory using the existing authorized
   memory list/history. Then call
   `GET /v1/memories/service-corrections/CANDIDATE_ID?external_user_id=OWNER&target_memory_id=TARGET_ID`.
   Preview returns exact candidate/target content, category and scope, source
   event/service identifiers, current incoming priority, accepted target priority,
   `target_version`, `review_version`, allowed `actions` and `apply_blockers`.
   Claim and winning ledger-revision IDs are supplied **by this admin preview**;
   this does not change ordinary retrieval/history's citation contract.
3. Review the actual preview. Send only these fields to
   `POST /v1/memories/service-corrections/CANDIDATE_ID/decision`:

   ```json
   {
     "external_user_id": "OWNER",
     "target_memory_id": "TARGET_UUID_FROM_PREVIEW",
     "target_version": "64_HEX_CHARACTERS_FROM_PREVIEW",
     "review_version": "64_HEX_CHARACTERS_FROM_PREVIEW",
     "action": "apply"
   }
   ```

   `action` is `apply` or `decline`. This example is a template, not executable
   input. Extra fields, replacement text, caller authority and scope overrides
   are rejected. The operator must explicitly assert that the selected target is
   the correct business fact: matching category/scope cannot prove field identity.

## Guarantees and limits

- Both actions recheck current tenant-admin permission, owner, source/event/job
  bindings and the complete preview under SQL locks. A current coherent generic
  claim/winning revision, same category and exact JSON-typed source scope are
  required. Typed preferences and agent-bound targets are excluded.
- `apply` retains the exact saved candidate text and original source event,
  archives the chosen target, writes record history, advances the existing claim
  and revision, queues the vector archive/upsert and records an operator audit.
  Candidate closure is in that **same SQL transaction**. No model matching or
  extraction is run. Its new zero-message conversation row identifies operator
  resolution, not a fabricated original conversation.
- `decline` marks the candidate dismissed with an actor/binding audit. It does
  not change, reactivate, delete or rewrite a memory, claim or source event.
- **Authority basis is current writer rules.** This candidate was pending, not
  an accepted revision with a frozen authority snapshot. The incoming source must
  still be enabled, its original API credential active with write/admin permission,
  and any current writer-key binding must still match. Lower current priority,
  stale/future observation, invalid/out-of-window validity or a pending-row expiry
  prevents apply. Decline may still be available. Existing accepted priorities are
  not retroactively reweighted. Attestation-policy overrides and expiring pending
  candidates are outside this apply path; their lifetime must not be guessed.
- Tokens bind candidate evidence/content, source state, current policy and
  source permission, target and winning revision. A decision is refused while
  those bound values differ from its preview. This is **current-state equality**,
  not a monotonic policy-generation or permanent preview-revocation guarantee:
  restoring all bound values (for example, disable then re-enable with unchanged
  rules and key binding) can make an earlier token valid again. No independent
  preview expiry is provided. Operators should fetch and review the current
  preview before deciding; token equality alone does not prove that review took
  place. Neither token is a record-history version number.
- An apply needs an embedding for the exact saved text, using the target's
  existing active model. It is calculated **before** row locks, then all guards
  are rechecked. This can call the configured paid embedding provider when an
  operator actually authorizes apply. No chat/extraction provider is used. An
  embedding/provider failure leaves state unchanged. Embedding cost may still be
  incurred if a race causes the subsequent SQL decision to be refused.
- Response `candidate_status=promoted` means SQL applied; `indexing_state=pending`
  is the original response's publication state. Vector delivery is separate.
  Existing retrieval publication guards remain in force. The original completed
  ingestion job/counters are not rewritten into a new processing result.
- Identical decisions replay the saved result without another mutation/embedding,
  after current operator/owner checks. Changed binding/action conflicts. Replay
  lasts only while the candidate's resolution receipt exists; explicit deletion
  removes it. Replayed indexing state is the original receipt, not live polling.
- A busy/stale transaction returns 409. Retry the **same** decision after a busy
  error; re-preview after a stale response. Do not change authority or rewrite
  the candidate to get an apply through. Policy refusal permits no override.
- Only the selected candidate closes. Other unresolved corrections in the same
  category/scope can keep retrieval unavailable. Source retention/deletion and
  existing review lifecycle outside this operation are not expanded.

## Operator responsibility

Use your own authorized synthetic owner and records for a beta integration.
The chosen target and apply/decline action require explicit operator review;
historical development incidents are not reusable customer fixtures. Do not
re-ingest, increase authority or edit SQL simply to clear a hold. Intentional
blocked-user access remains enforced independently of this operation.
