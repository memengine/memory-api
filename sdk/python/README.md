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


if result.was_stored:
    print(f"Memory queued: {result.job_id}")
elif result.nothing_to_extract:
    print("Conversation had no storable information")
elif result.status == "blocked":
    print(f"Blocked: {result.blocked_reason}")
```

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
