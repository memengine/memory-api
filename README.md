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

Choose Python or TypeScript. Run the example from the repository root if you're using the generated test credentials. Each run creates a fresh synthetic account so you can repeat the walkthrough. In your app, reuse the account ID for the same customer.

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
from uuid import uuid4

from memoryos import Memory

api_key = os.getenv("MEMORYOS_API_KEY")
if not api_key:
    credentials = json.loads(Path("beta/credentials.local.json").read_text())
    api_key = credentials["clients"]["billing"]["api_key"]

demo_account = f"demo-account-{uuid4()}"

with Memory(
    api_key=api_key,
    base_url=os.getenv("MEMORYOS_API_URL", "http://127.0.0.1:18000"),
) as memory:
    saved = memory.add(
        external_user_id=demo_account,
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
            external_user_id=demo_account,
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
import { randomUUID } from "node:crypto";
import { readFileSync } from "node:fs";
import { MemoryOS } from "memoryo-sdk";

const apiKey = process.env.MEMORYOS_API_KEY ??
  JSON.parse(readFileSync("beta/credentials.local.json", "utf8")).clients.billing.api_key;

const memory = new MemoryOS(
  apiKey,
  process.env.MEMORYOS_API_URL ?? "http://127.0.0.1:18000",
);

const demoAccount = `demo-account-${randomUUID()}`;
const saved = await memory.add({
  externalUserId: demoAccount,
  messages: [{ role: "user", content: "This account has subscription plan Growth." }],
});
if (!saved.jobId) throw new Error(`Memory was not queued: ${saved.blockedReason ?? saved.status}`);

const job = await memory.waitForJob(saved.jobId);
if (!job.succeeded) throw new Error(`Memory processing stopped: ${job.status}`);

for (let attempt = 0; attempt < 15; attempt++) {
  const context = await memory.get({
    query: "What subscription plan does this account have?",
    externalUserId: demoAccount,
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

Start with one question your chatbot already handles, such as “What plan is my account on?” Add the MemoryOS read in your **server-side chat handler**, after you authenticate the customer and **before** you call your model. Get the account ID from your authenticated session or database. Use that same ID when saving account updates.

This Python example uses the SDK installed above. Set `MEMORYOS_API_URL` and a read-capable `MEMORYOS_API_KEY` on your chatbot server.

```python
import os

from memoryos import Memory, MemoryOSError

chat_memory = Memory(
    api_key=os.environ["MEMORYOS_API_KEY"],
    base_url=os.environ["MEMORYOS_API_URL"],
)


def answer_account_question(account_id, question, generate_reply):
    try:
        context = chat_memory.get(
            external_user_id=account_id,
            query=question,
            categories=["fact"],
            format="json",
        )
    except MemoryOSError:
        return "I can't access your account details right now."

    if context.memory_visibility:
        return "I can't confirm your account details yet. Please check back shortly."
    if not context.items:
        return "I couldn't find account details to answer that."

    records = [
        {
            "id": record.id,
            "content": record.content,
            "source_event_id": record.source_event_id,
        }
        for record in context.items
    ]
    return generate_reply(question, account_records=records)
```

Pass your existing model wrapper as `generate_reply`. Have it include `account_records` in the prompt as reference data. Instruct it to use those records for current account facts, cite the record IDs for the facts it uses, and say when the records don't answer the question. The handler returns a fallback before calling the model when context is withheld.

**Write updates in the service that owns the fact.** For subscription plans, call `add()` from your Billing webhook or event consumer after your app accepts the event. Configure that server with a write-capable key and the registered Billing service name. Use the original event ID as the idempotency key and record the job ID for background polling. See the service-write examples for [Python](sdk/python/README.md#multi-service-mode-support-billing-crm-and-product-services) and [TypeScript](sdk/typescript/README.md#multi-service-mode-support-billing-crm-and-product-services).

Test with one synthetic account. Send a Billing update, let the job finish, then ask the chatbot the question in a fresh conversation.

### 4. Try it with two sources

Billing says a test customer is on Growth. CRM reports a correction to Scale. What should your support agent say when the customer asks, “What plan am I on?”

Run the [Billing and CRM walkthrough](beta/README.md#one-two-service-integration-example) to see what MemoryOS returns for the same account, which source supplied the current record, and whether a correction needs review before your agent can use the affected context.

[Contributing](CONTRIBUTING.md) · [Development](DEVELOPMENT.md) · [Security](SECURITY.md) · [MIT license](LICENSE)
