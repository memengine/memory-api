# MemoryOS Python SDK

```bash
pip install memoryos
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
from memoryos import Memory

client = Memory(api_key="mem_live_xxx", base_url="https://api.memoryos.io")

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

## Quickstart

1. Create an API key in the MemoryOS dashboard.
2. Install the package with `pip install memoryos`.
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

## Handling Degraded Responses

```python
result = client.get(query="...", external_user_id="...")

if result.is_passthrough:
    # MemoryOS quota exhausted or system degraded
    # Your AI still works — just without memory context
    system_prompt = BASE_SYSTEM_PROMPT
elif result.is_degraded:
    # Serving from cache — fewer memories than usual
    # Still worth using
    system_prompt = BASE_SYSTEM_PROMPT + result.system_prompt_addition
else:
    # Full memory context
    system_prompt = BASE_SYSTEM_PROMPT + result.system_prompt_addition

# Always call your LLM — never skip because of MemoryOS state
response = llm.complete(system_prompt, user_message)
```

```python
result = client.add(messages=messages, external_user_id="...")

if result.processing_status == "delayed":
    # Queue is busy — memory will be stored but takes longer
    # ETA in seconds:
    eta = result.processing_eta_seconds
    # Log it — do not surface to end user
```
