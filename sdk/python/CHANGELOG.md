# Changelog

## 0.1.2 - 2026-11-03

- Include existing typed visibility holds and degraded-read status in sync/async clients.
- Document explicit local backend URLs, hold-before-answer handling and operator HTTP limits.
- Experimental transports remain server-gated; no flags enabled by SDK installation.

## 0.1.0

- Initial Python SDK for MemoryOS
- Added synchronous `Memory` client using `httpx.Client`
- Added asynchronous `AsyncMemory` client using `httpx.AsyncClient`
- Added Pydantic v2 request and response models
- Added typed error mapping for auth, rate limit, and not-found failures
- Added retry handling for `429` and `5xx` responses
- Added example integrations for FastAPI, OpenAI, Anthropic, AutoGen, and LangChain
