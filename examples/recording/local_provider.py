"""Launch a beta process with only its model-provider calls mocked."""

import json
import os
import re
import sys
import time
from uuid import UUID

from api.services.embedding_service import EmbeddingService
from api.services.llm_service import LLMService


async def mock_completion(self, system_prompt, user_message, temperature, max_tokens, response_format):
    from api.services.llm_service import LLMResponse

    statement = re.search(r"Subscription plan is Growth\.|Correction: Scale replaces the prior tier\.", user_message, re.IGNORECASE)
    if "conflict resolution engine" in system_prompt.lower():
        result = {"action": "KEEP_BOTH", "reasoning": "Fixture classifier leaves the correction unresolved."}
    elif "pass 1 in a two-pass" in system_prompt.lower():
        result = {"entities": [], "relationships": [], "notes": ""}
    elif statement:
        statement = statement.group().strip()
        result = {"memories": [{
            "content": statement,
            "category": "fact",
            "importance_score": 8,
            "confidence": 0.99,
            "claim_state": "correction" if statement.startswith("Correction:") else "asserted",
        }], "nothing_to_extract": False}
    else:
        raise ValueError("The demo provider received an unsupported prompt.")
    return LLMResponse(json.dumps(result), "openai", "demo-extractor", 1, 1, 2, 0)


def mock_embedding(self, text, model, tenant_id):
    # Both plan texts match the query, while the two values remain distinct.
    words = text.lower()
    vector = [0.0] * model.dimensions
    if "growth" in words and "scale" not in words:
        vector[0] = 1.0
    elif "scale" in words:
        vector[1] = 1.0
    else:
        vector[0] = vector[1] = 0.70710678118
    return vector


def main():
    if (os.getenv("MEMORYOS_DEMO_MOCK"), os.getenv("OPENAI_API_KEY")) != ("1", "local-demo-placeholder"):
        raise SystemExit("Use the dedicated demo Compose overlay.")
    LLMService._call_openai = mock_completion
    EmbeddingService._embed_with_model_sync = mock_embedding
    mode, *args = sys.argv[1:]
    if mode == "api":
        import uvicorn

        uvicorn.run("api.main:app", host="0.0.0.0", port=8000)
    elif mode == "worker":
        from api.celery_app import celery_app

        celery_app.worker_main(["worker", "--pool=solo", "--loglevel=warning", "-Q", ",".join(args)])
    elif mode == "publisher":
        from api.celery_app import celery_app

        tenant = str(UUID(os.environ["LOCAL_VECTOR_TENANT_ID"]))
        while True:
            celery_app.send_task("api.tasks.vector_sync_tasks.process_outbox",
                                 kwargs={"tenant_id": tenant}, queue="celery", expires=1)
            time.sleep(0.5)
    else:
        raise SystemExit(f"Unsupported demo process: {mode}")


if __name__ == "__main__":
    main()
