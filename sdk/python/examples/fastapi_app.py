import os

from fastapi import FastAPI

from memoryos import AsyncMemory


app = FastAPI()
memory_client = AsyncMemory(
    api_key=os.environ["MEMORYOS_API_KEY"],
    base_url=os.getenv("MEMORYOS_BASE_URL", "http://127.0.0.1:8000"),
)


@app.get("/assistant-context")
async def assistant_context(query: str, external_user_id: str) -> dict[str, object]:
    results = await memory_client.get(
        query=query,
        external_user_id=external_user_id,
        limit=5,
        context_max_tokens=300,
    )
    return {
        "quota_mode": results.quota_mode,
        "system_prompt_addition": results.system_prompt_addition if results.has_context else "",
        "context_token_count": results.context_token_count,
        "memories": [item.content for item in results.items],
    }
