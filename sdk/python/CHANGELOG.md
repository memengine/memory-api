# Changelog

## 0.1.0

- Initial production-ready Python SDK for MemoryOS
- Added synchronous `Memory` client using `httpx.Client`
- Added asynchronous `AsyncMemory` client using `httpx.AsyncClient`
- Added Pydantic v2 request and response models
- Added typed error mapping for auth, rate limit, and not-found failures
- Added retry handling for `429` and `5xx` responses
- Added example integrations for FastAPI, OpenAI, Anthropic, AutoGen, and LangChain
