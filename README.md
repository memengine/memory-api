# MemoryOS

**Memory for AI agents that tracks what changed and where it came from.**

Building a chatbot or agent? Use MemoryOS to save useful information from conversations and services, then fetch the current records when your agent needs them. Each record can carry its source and history. When an update needs review, your app can see that before it uses the affected context.

[Quickstart](#quickstart) · [Python SDK](sdk/python/README.md) · [TypeScript SDK](sdk/typescript/README.md) · [API docs](https://docs.memoryo.dev/)

## Quickstart

### 1. Start the API

If you already have a MemoryOS server, set `MEMORYOS_API_URL` and `MEMORYOS_API_KEY` on your application server.

To run your own copy, use Git and Docker Compose.

```bash
git clone https://github.com/memengine/memory-api.git
cd memory-api
```

Follow the [Docker setup](beta/README.md#fresh-local-startup) to start the API, create a test workspace and start its publisher. Add your OpenAI API key to `beta/beta.env` before starting the application containers so they can extract and index memories. The local API runs at `http://127.0.0.1:18000`. The examples below use the test workspace's Billing key from `beta/credentials.local.json` if you haven't set an API key.

### 2. Save and read a memory

Choose Python or TypeScript. Run the example from the repository root if you're using the generated test credentials.

<details open>
<summary><strong>Python</strong></summary>

Install the SDK.

```bash
python -m pip install memoryo-sdk==0.1.2
```

Save this as `example.py` and run `python example.py`.

```python
import json
import os
import time
from pathlib import Path

from memoryos import Memory

api_key = os.getenv("MEMORYOS_API_KEY")
if not api_key:
    credentials = json.loads(Path("beta/credentials.local.json").read_text())
    api_key = credentials["clients"]["billing"]["api_key"]

with Memory(
    api_key=api_key,
    base_url=os.getenv("MEMORYOS_API_URL", "http://127.0.0.1:18000"),
) as memory:
    saved = memory.add(
        external_user_id="demo-account",
        messages=[{"role": "user", "content": "This account has subscription plan Growth."}],
    )
    if not saved.job_id:
        raise RuntimeError(f"Memory was not queued: {saved.blocked_reason or saved.status}")

    job = memory.wait_for_job(saved.job_id)
    if not job.succeeded:
        raise RuntimeError(f"Memory processing stopped: {job.status}")

    for _ in range(15):
        context = memory.get(
            query="What subscription plan does this account have?",
            external_user_id="demo-account",
        )
        if not context.memory_visibility or context.memory_visibility.state != "processing":
            break
        time.sleep(2)

    if context.memory_visibility:
        print("Memory context is unavailable. Check again before answering.")
    else:
        for record in context.items:
            print(record.content)
```

</details>

<details>
<summary><strong>TypeScript / Node.js</strong></summary>

Install the SDK.

```bash
npm install memoryo-sdk@0.1.3
```

Save this as `example.mjs` and run `node example.mjs`.

```javascript
import { readFileSync } from "node:fs";
import { MemoryOS } from "memoryo-sdk";

const apiKey = process.env.MEMORYOS_API_KEY ??
  JSON.parse(readFileSync("beta/credentials.local.json", "utf8")).clients.billing.api_key;

const memory = new MemoryOS(
  apiKey,
  process.env.MEMORYOS_API_URL ?? "http://127.0.0.1:18000",
);

const saved = await memory.add({
  externalUserId: "demo-account",
  messages: [{ role: "user", content: "This account has subscription plan Growth." }],
});
if (!saved.jobId) throw new Error(`Memory was not queued: ${saved.blockedReason ?? saved.status}`);

const job = await memory.waitForJob(saved.jobId);
if (!job.succeeded) throw new Error(`Memory processing stopped: ${job.status}`);

for (let attempt = 0; attempt < 15; attempt++) {
  const context = await memory.get({
    query: "What subscription plan does this account have?",
    externalUserId: "demo-account",
  });
  if (!context.memoryVisibility) {
    for (const record of context.items) console.log(record.content);
    break;
  }
  if (context.memoryVisibility.state !== "processing" || attempt === 14) {
    console.log("Memory context is unavailable. Check again before answering.");
    break;
  }
  await new Promise((resolve) => setTimeout(resolve, 2000));
}
```

</details>

A successful read prints the saved plan. The example waits for the write job and briefly retries retrieval while indexing catches up. A correction that needs review can be resolved through the [operator API](beta/OPERATOR.md).

### 3. Connect your agent

1. **Save updates** after a conversation or when a service reports a change. Use the same user or account ID from your application on reads and writes.
2. **Fetch context** before the agent answers. If MemoryOS reports that context is processing or needs review, use your app's fallback.
3. **Add a second source** when you need to reconcile updates from different systems. Try the [billing and CRM example](beta/README.md#one-two-service-integration-example) to see source priority, review and history together.

Load the API key on your server. Use the [Python](sdk/python/README.md) or [TypeScript guide](sdk/typescript/README.md) for job polling, provenance and more examples.

[Contributing](CONTRIBUTING.md) · [Development](DEVELOPMENT.md) · [Security](SECURITY.md) · [MIT license](LICENSE)
