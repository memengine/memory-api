# SDK Verification Status

Date: 2026-03-25

## Verified Here

- SDK public parameter cleanup
  - Status: passed
  - Verified behavior:
    - Public method signatures contain zero `user_id` parameters: PASS
    - Compatibility shim contains `user_id` for Python backward compatibility: PASS
    - `external_user_id` is present in all Python public method signatures that changed: PASS
    - TypeScript `userId` is cleared from public source for the cleaned methods: PASS
    - Python backward-compat deprecation path emits `DeprecationWarning`: PASS

- `pip install -e ./sdk/python`
  - Status: passed
  - Note: this required elevated execution because the local Windows Python environment was installed under `C:\Users\adity` and `pip` hit a permission error before reaching the SDK package.

- Local TypeScript package install
  - Status: passed
  - Verification method: created a clean consumer folder under `.verification/ts-install` and ran `npm install ../../sdk/typescript`

- Python SDK typed auth failure
  - Status: passed
  - Verified behavior: `401` response raised `AuthError` with `status_code=401` and `code='AUTH_001'`

- Python SDK `429` retry and backoff
  - Status: passed
  - Verified behavior: two `429` responses triggered retries with backoff delays `[1, 2]`, then the request succeeded on attempt 3

- TypeScript SDK typed auth failure
  - Status: passed
  - Verified behavior: `401` response raised `AuthError` with `statusCode=401` and `code='AUTH_001'`

- TypeScript SDK `429` retry and backoff
  - Status: passed
  - Verified behavior: two `429` responses triggered retries with backoff delays `1000ms` and `2000ms`, then the request succeeded on attempt 3

- TypeScript SDK strict typing and bundle output
  - Status: passed
  - Verified commands:
    - `npm run typecheck`
    - `npm run build`

## Not Verified Here

- Python 15-line quickstart against the production API
  - Reason: requires a live MemoryOS API key and permission to hit production

- TypeScript quickstart against the production API
  - Reason: requires a live MemoryOS API key and permission to hit production

- Real production `429` handling for both SDKs
  - Reason: should be tested with a dedicated low-rate-limit MemoryOS test key to avoid spamming or throttling a primary production key

## Manual Verification Steps

### Python quickstart

```bash
python - <<'PY'
from memoryos import Memory

client = Memory(api_key="mem_live_xxx", base_url="https://api.memoryos.io")
result = client.add(
    external_user_id="student_44821",
    messages=[{"role": "user", "content": "I prefer Python and FastAPI"}],
)
memories = client.get("programming preferences", "student_44821", 3)
print(result.status, result.quota_mode)
print(memories.quota_mode)
print([item.content for item in memories.items])
client.close()
PY
```

Expected:
- add call returns `queued`, `blocked`, or `passthrough`
- retrieve call exposes `quota_mode`
- if quota mode is `PASSTHROUGH`, the caller should skip memory injection instead of treating it as an error

### TypeScript quickstart

```bash
node --input-type=module - <<'JS'
import { MemoryOS } from "@memoryos/sdk";

const client = new MemoryOS("mem_live_xxx", "https://api.memoryos.io");
const result = await client.add(
  [{ role: "user", content: "I prefer Python and FastAPI" }],
  "student_44821",
);
const memories = await client.get("programming preferences", "student_44821", 3);
console.log(result.status, result.quotaMode);
console.log(memories.quotaMode);
console.log(memories.items.map((item) => item.content));
JS
```

Expected:
- add call returns `queued`, `blocked`, or `passthrough`
- retrieve call exposes `quotaMode`
- if quota mode is `PASSTHROUGH`, the caller should proceed without memory context

### Production `429` verification

Use a temporary API key with a very low rate limit, then call `get()` repeatedly until the SDK hits a `429`.

Expected:
- retries happen automatically
- exponential backoff is visible
- request eventually succeeds if the retry window clears
- if the retry window does not clear, the SDK raises `RateLimitError`

## Verification Note

The earlier grep-based check that expected zero `user_id` matches in Python SDK source was incorrect.

Correct interpretation:
- public method signatures should not expose `user_id`
- the Python compatibility shim is expected to retain `user_id` internally so existing keyword-argument calls can warn and continue
- `external_user_id` / `externalUserId` should be the only public parameter shape for the cleaned methods
