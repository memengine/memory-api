# Connect your agent

Start with one question your chatbot already handles, such as “What plan is my account on?” Add the MemoryOS read in your **server-side chat handler**, after you authenticate the customer and **before** you call your model. Get the account ID from your authenticated session or database. Use that same ID when saving account updates.

This Python example uses the SDK from the [quickstart](quickstart.md). Set `MEMORYOS_API_URL` and a read-capable `MEMORYOS_API_KEY` on your chatbot server.

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

**Write updates in the service that owns the fact.** For subscription plans, call `add()` from your Billing webhook or event consumer after your app accepts the event. Configure that server with a write-capable key and the registered Billing service name. Use the original event ID as the idempotency key and record the job ID for background polling. See the service-write examples for [Python](../sdk/python/README.md#multi-service-mode-support-billing-crm-and-product-services) and [TypeScript](../sdk/typescript/README.md#multi-service-mode-support-billing-crm-and-product-services).

Test with one synthetic account. Send a Billing update, let the job finish, then ask the chatbot the question in a fresh conversation.
