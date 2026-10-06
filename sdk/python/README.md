# MemoryOS Python SDK

```bash
pip install memoryo-sdk
```

## Start simple: solo builders and small teams

> **For whom:** solo developers, MVPs, small SaaS apps, and single-agent products.
> You do **not** need `event_id`, `run_id`, service writers, or authority rules to start. MemoryOS creates safe internal source metadata automatically.

```python
import os
from memoryos import Memory

mem = Memory(api_key=os.environ["MEMORYOS_API_KEY"])

write = mem.add(
    external_user_id="user_123",
    messages=[
        {"role": "user", "content": "I prefer short answers when debugging."},
        {"role": "assistant", "content": "Got it. I will keep debugging replies concise."},
    ],
)

if not write.job_id:
    raise RuntimeError(f"Memory write was not queued: {write.status}")
job = mem.wait_for_job(write.job_id)
if not job.succeeded:
    raise RuntimeError(f"Memory write failed: {job.error_summary or job.status}")

context = mem.get(
    external_user_id="user_123",
    query="How should I answer this user?",
)

if context.has_context:
    system_prompt = BASE_SYSTEM_PROMPT + "\n\n" + context.system_prompt_addition
```

This is the recommended first integration. Add source metadata only when more than one backend service writes memories for the same users.

### Read withholding status (source checkout)

`Memory.get()` and `AsyncMemory.get()` preserve `result.memory_visibility`
as an exported `MemoryVisibility` model, or `None` when the server supplies no
hold. Its `state` is `processing` or `unavailable`; its `scope` is
`user_learned_context` or `affected_context`. An older response without `scope`
defaults to the conservative whole learned-context scope. Invalid status values
raise a validation error rather than silently dropping a hold.

`result.is_degraded` preserves the backend flag even when quota mode is `FULL`.
A healthy circuit and available quota do not prove that stored context is current.
With `affected_context`, unaffected eligible items can still be returned. With a
whole-context hold, learned items are withheld. The backend's prompt-ready notice
is preserved, and the typed status remains available when the prompt budget is
zero. `has_context` means a prompt addition exists; a withholding notice can count
as that addition even with no memory items. It is not proof that a preference is
current, a request for clarification, or confirmation of storage.

This is additive SDK transport support, not scope inference. On an operator-enabled
tenant (`RUNTIME_PREFERENCE_DISCOVERY_ENABLED`, default false), ordinary current
reads can discover declared property keys from existing search nominations and
the tenant catalog. Without application scope, a relevant property with scoped
bindings is withheld and returned as `result.preference_scope_request` (exported
`PreferenceScopeRequest`), with `state="application_scope_required"`, `property_keys`
and `available_scopes`. Ask the scope question in your existing chat, then send the
explicitly selected scope and returned keys in `preference_context` on a subsequent
read. Do not default to global, infer scope from query wording, or treat this as a
preference write or permission change. Typed status survives zero prompt budget.
With discovery off, generic reads still exclude declared preferences. No automatic
polling or additional read-time model call is added. This source change is not a
claim that a new package has been published.

## Opt-in scoped preferences (local, unreleased)

This source checkout exposes an application-declared contract, **not automatic
natural-language scope/uncertainty recognition**. Published `0.1.1` does not
include these fields. The backend feature remains disabled by default; disabled
requests fail with `409 / PREF_409` rather than falling back to unscoped writes.

```python
from memoryos import PreferenceClaimRequest, PreferenceReadContext

scope = {"kind": "context", "namespace": "project", "key": "release-check"}
quote = "For Release Check, use Python for examples."
write = mem.add(
    messages=[{"role": "user", "content": quote}],
    external_user_id="user_123",
    preference_claims=[PreferenceClaimRequest(
        property_key="examples.language", value="Python", applicability=scope,
        turn_index=0, quote=quote,
    )],
)
# Queued, not confirmed storage. Check status separately, not during streaming.
context = mem.get(
    query="Which language should examples use?", external_user_id="user_123",
    preference_context=PreferenceReadContext(
        applicability=scope, property_keys=["examples.language"],
    ),
)
```

`AsyncMemory` accepts the same keyword arguments with `await`. Normal calls that
omit these options retain the existing flow. When declarations are supplied,
the job processes only those declarations, not other facts in the transcript.
Property/scope keys must be stable and application-owned; never guess unknown
scope as global. Declarations stay `client_asserted`, authority capped at 20.
Do not combine them with `source` or `agent_id`, or explicit reads with `as_of`.

For intentional replacement, use `commitment="replacement"` plus
`replaces_memory_id` and the exact `provenance["preference_version"]` returned
by the backend. Tentative uncertainty stays pending without withholding an
existing default; review actions never activate candidate text.
Existing job status preserves raw `extraction_metadata` and provides
`source_reviews_capacity_rejected`; completion alone does not mean storage.
Immediate reads may precede job completion. There is no automatic polling,
new model call, authority decision, or read-after-write guarantee in the SDK.
See [the full contract](../../docs/explicit_preference_contract.md).

### An explicitly unresolved current choice

The local, unreleased SDK also accepts `choice_state="current_choice_unresolved"`
with `commitment="uncertain"`, known applicability, and the existing winner's exact
ID/version. This is an application declaration, not interpretation of the user's
wording. Use it only when your integration has established that the current choice
is unresolved, not for merely tentative interest in another value.

```python
# current_id/current_version are backend-issued values from an owned management
# or prior retrieval result, not guesses or assistant-generated references.
quote = "For Release Check, I am undecided between my current C++ default and Python."
write = mem.add(
    messages=[{"role": "user", "content": quote}], external_user_id="user_123",
    preference_claims=[PreferenceClaimRequest(
        property_key="examples.language", value="Python", applicability=scope,
        turn_index=0, quote=quote, commitment="uncertain",
        choice_state="current_choice_unresolved",
        replaces_memory_id=current_id, replaces_version=current_version,
    )],
)
```

The backend checks the exact identity, evidence, ownership, currentness, authority
and version. A queued write does not prove the choice was withheld: inspect raw
job `extraction_metadata["preference_choices"]` when processing confirmation is
needed. Do not add an automatic wait to the answer-generation path.

Tenant `get()` returns exported `PreferenceReadDecision` objects in
`context.preference_decisions` and preserves `context.clarification_question`.
Old servers without the field return an empty decision list. Check decisions
independently of `context.has_context` or `context.items`: an unresolved choice
can be the only returned information, or coexist with usable unrelated facts.

```python
context = mem.get(
    query="Which language should examples use?", external_user_id="user_123",
    preference_context={"applicability": scope, "property_keys": ["examples.language"]},
)
for decision in context.preference_decisions:
    # Supply this structured outcome to your existing chat workflow. The SDK
    # does not render UI, alter your agent prompt, choose a value, or resolve it.
    print(decision.property_key, decision.applicability, decision.state)
```

Dismissal/expiry of a review does not restore the withheld default. A matching
`keep_current` review can reaffirm the existing valid value; an exact version-bound
replacement can resolve the choice. No authority is raised and no candidate text
is activated by review. The backend remains disabled by default; older servers
that reject a new declaration surface that error, not a retry with fields removed.
This capability is not yet published and does not add automatic semantic admission.

## Multi-service mode: support, billing, CRM, and product services

> **For whom:** teams where multiple services can write facts about the same user.
> Use this when you need auditability, deduplication, conflict handling, or source-of-truth routing.

After registering the service writers in the Tenant Dashboard, teams can pass their registered `source.service` keys:

```python
from memoryos import Memory

mem = Memory(api_key=os.environ["MEMORYOS_API_KEY"])

mem.add(
    external_user_id="cust_123",
    messages=[{"role": "assistant", "content": "Customer is on the Growth plan."}],
    source=Memory.source(
        "billing-service",
        event_id="invoice_evt_8842",  # optional but useful for dedupe/audit
        scope={"workspace_id": "ws_123"},
        evidence=[{"source_type": "billing_record", "reference": "invoice_evt_8842"}],
    ),
)

mem.add(
    external_user_id="cust_123",
    messages=[{"role": "assistant", "content": "Customer support previously saw the Starter plan."}],
    source=Memory.source("support-service"),  # event_id and observed_at are generated
)
```

Production teams should also bind dedicated API keys to those writers. Then Billing, Support, CRM, and other services can have different authority rules without changing the basic SDK flow.

## New in this release

### Checking if a conversation produced memories

```python
result = client.add(
    messages=messages,
    external_user_id="user_1",
)


if result.was_queued:
    print(f"Memory queued: {result.job_id}")
elif result.nothing_to_extract:
    print("Conversation had no storable information")
elif result.status == "blocked":
    print(f"Blocked: {result.blocked_reason}")
```

`was_stored` remains as a deprecated compatibility alias. A queued request is
not confirmed storage; call `wait_for_job()` and inspect
`job.created_memory_ids` when you need the exact records created.
`wait_for_job()` continues polling through the backend's retryable `failed`
state and returns when the job completes or exhausts retries as `dead`.

When retrieval returns `result.clarification`, show its question in your
existing chat and send the user's answer back with
`client.answer_clarification(...)`. MemoryOS validates identity, expiry, and
the conflict transition on the backend.

### Source reviews (0.1.1)

With source-merge containment deployed, reviews can also represent a blocked automatic
merge, not only uncertainty. The current memory remains active and the incoming candidate
is pending. This backend behavior uses the same SDK methods; it does not require a new
package version. A completed ingestion job can create zero memories. Read the job outcome
and present the returned review in your existing chat; do not report the merge as stored.

When recognized uncertainty has no usable alternative, `result.source_reviews`
may contain a non-activating review. Present its question and allowed actions in
your existing chat. Submit the actual user's selection, never a model decision:

```python
review = result.source_reviews[0] if result.source_reviews else None
if review:
    # user_action is the user's choice from review.actions in your chat.
    resolution = client.answer_source_review(
        review.id,
        external_user_id="user_1",
        version=review.version,
        action=user_action,
    )
```

`keep_current` and `dismiss` close the pending interpretation without changing
stored memory or raising authority. `restate` leaves it pending and returns
`resolved=False`, `next_step="add_memory"`: obtain the user's actual new statement
and submit it through normal `add()` ingestion. It is not completed storage.
The same methods are available with `await` on `AsyncMemory`.
Reviews are bounded to three recent items, expire seven days after the server's
last observation, and are absent for historical retrieval. This fallback does
not fix semantic misclassification or suppress all pending-property context.

### Filtering by time

```python
recent = client.get(
    query="what has the user been working on",
    external_user_id="user_1",
    time_filter_days=7,
)
```

### Context format options

```python
import json

result = client.get(
    query="user preferences",
    external_user_id="user_1",
    format="json",
    context_max_tokens=300,
)

preferences = json.loads(result.system_prompt_addition)
```

### Understanding memory importance trends

```python
result = client.get(query="technical preferences", external_user_id="user_1")

if result.has_context:
    system_prompt = BASE_SYSTEM_PROMPT + result.system_prompt_addition

for memory in result.items:
    print(memory.content)
    print(f"  trend: {memory.importance_trend}")
    if memory.is_hot:
        print("  [hot tier - served from fast cache]")
```

### Reading an EdTech profile

If your tenant has the EdTech domain schema enabled, normal `get()` calls already include domain-aware tutoring context in `system_prompt_addition`. Use `get_edtech_profile()` only when your app needs the full structured student profile for UI or analytics.

```python
profile = client.get_edtech_profile(external_user_id="student_44821")

if profile and profile.has_learning_profile:
    print(profile.grade_level)
    print(profile.explanation_style)
    print(profile.weak_topics[:3])

if profile and profile.has_exam_context:
    print(profile.exam_name, profile.exam_date)
```

## Webhook Signature Verification

```python
import hashlib
import hmac


def verify_memoryos_webhook(body_bytes: bytes, signature_header: str, webhook_secret: str) -> bool:
    expected = hmac.new(
        webhook_secret.encode("utf-8"),
        body_bytes,
        hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(expected, signature_header)


signature = request.headers["X-MemoryOS-Signature"]
if not verify_memoryos_webhook(request.body, signature, WEBHOOK_SECRET):
    raise ValueError("Invalid MemoryOS webhook signature")
```

```python
import os

from memoryos import Memory

client = Memory(api_key=os.environ["MEMORYOS_API_KEY"], base_url="https://api.memoryo.dev")

result = client.add(
    external_user_id="student_44821",
    messages=[{"role": "user", "content": "I prefer Python and FastAPI"}],
)

memories = client.get(
    query="programming preferences",
    external_user_id="student_44821",
    limit=3,
)

print(result.status, result.quota_mode)
print(memories.quota_mode)
print(memories.items[0].content if memories.items else "No memories found")
client.close()
```

`MemoryOS` gives you a typed, low-dependency client for the production API.

- Sync client: `Memory`
- Async client: `AsyncMemory`
- Runtime dependencies: only `httpx` and `pydantic`
- Built-in retries on `429` and `5xx`
- Typed results for add, search, list, delete, and export

## Sync, async, and domain schemas

`Memory` and `AsyncMemory` use the same MemoryOS backend.

Use `Memory` in normal blocking Python code:

```python
import os

from memoryos import Memory

client = Memory(api_key=os.environ["MEMORYOS_API_KEY"])
result = client.get(query="customer preference", external_user_id="cust_123")
```

Use `AsyncMemory` in async apps such as FastAPI or async workers:

```python
import os

from memoryos import AsyncMemory

client = AsyncMemory(api_key=os.environ["MEMORYOS_API_KEY"])
result = await client.get(query="customer preference", external_user_id="cust_123")
```

Async memory is not a different memory engine. It is the same API with `await`.

Domain schemas are tenant settings, not separate SDK methods. If your tenant enables EdTech or Customer Support, normal `add()` and `get()` calls automatically use the domain overlay.

```text
General tenant:
  add() -> general extraction
  get() -> general memory context

EdTech tenant:
  add() -> general extraction + EdTech overlay
  get() -> general memory context + tutoring context

Support tenant:
  add() -> general extraction + Support overlay
  get() -> general memory context + customer support context
```

For Support agents, the runtime flow is still:

```python
memories = client.get(
    query=user_message,
    external_user_id=customer_id,
    agent_id="support-bot",
)

system_prompt = BASE_SUPPORT_PROMPT
if memories.has_context:
    system_prompt += "\n\n" + memories.system_prompt_addition
```

MemoryOS remembers customer context. Your own CRM, order, billing, ticket, or banking tools still provide live system truth and actions.

## Retrieval feedback

`get()` returns a `retrieval_id`. Keep it with the model response for that turn. If the agent used the memory successfully, was corrected by the user, or had to ask for missing context, send feedback back to MemoryOS.

```python
result = client.get(
    query=user_message,
    external_user_id="customer_123",
)

# Build your model prompt from result.system_prompt_addition, then answer the user.

if result.retrieval_id:
    client.feedback(
        retrieval_id=result.retrieval_id,
        outcome="used_successfully",
        used_memory_ids=[memory.id for memory in result.items],
        agent_confidence=0.9,
    )
```

When the user corrects the agent or the agent had to ask a clarification, include the correction. MemoryOS records the signal and can queue asynchronous retrospective extraction.

```python
if result.retrieval_id:
    feedback = client.feedback(
        retrieval_id=result.retrieval_id,
        outcome="user_corrected",
        correction="Actually, the user prefers Hindi replies, not English.",
    )

    if feedback.queued_retrospective_extraction:
        print("MemoryOS queued a background correction pass")
```
## Quickstart

1. Create an API key in the MemoryOS dashboard.
2. Install the package with `pip install memoryo-sdk`.
3. Import `Memory` for sync code or `AsyncMemory` for async apps.
4. Pass your API key as `api_key="mem_live_..."`.
5. Override `base_url` only for local or self-hosted environments.
6. Call `add()` with the tenant's `external_user_id`.
7. Check `result.status` and `result.quota_mode` to handle `blocked` or `passthrough` gracefully.
8. Call `get()` with the same `external_user_id` to retrieve relevant memories for a query.
9. Use `retrieve_result.system_prompt_addition` only when `retrieve_result.quota_mode != "PASSTHROUGH"`.
10. Call `list()` to page through stored memories.
11. Call `delete()` to archive or hard-delete a memory.
12. Call `export()` to download the user export bundle.
13. All calls raise typed SDK errors on failure.
14. `401` maps to `AuthError`.
15. `404` maps to `NotFoundError`.
16. `429` maps to `RateLimitError`.
17. Use the `examples/` folder for common integration patterns.
18. If a domain schema is enabled for the tenant, keep using the same `add()` and `get()` calls.

## Handling Degraded Responses

```python
result = client.get(query="...", external_user_id="...")

if result.is_passthrough:
    # MemoryOS quota exhausted or system degraded
    # Your AI still works - just without memory context
    system_prompt = BASE_SYSTEM_PROMPT
elif result.is_degraded:
    # Serving from cache - fewer memories than usual
    # Still worth using
    system_prompt = BASE_SYSTEM_PROMPT + result.system_prompt_addition
else:
    # Full memory context
    system_prompt = BASE_SYSTEM_PROMPT + result.system_prompt_addition

# Always call your LLM - never skip because of MemoryOS state
response = llm.complete(system_prompt, user_message)
```

```python
result = client.add(messages=messages, external_user_id="...")

if result.processing_status == "delayed":
    # Queue is busy - memory will be stored but takes longer
    # ETA in seconds:
    eta = result.processing_eta_seconds
    # Log it - do not surface to end user
```
