# MemoryOS

**The governed context layer for production AI agents.**

MemoryOS helps AI systems maintain trustworthy, current context across sessions, services, and
agents. It does more than persist notes: it controls how learned state is extracted, corrected,
versioned, shared, retrieved, and audited.

[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)
![Python 3.12+](https://img.shields.io/badge/python-3.12%2B-blue)
![FastAPI](https://img.shields.io/badge/FastAPI-0.115%2B-009688)

## The Problem Is Not Forgetting Alone

Files, skills, vector stores, and note systems can preserve information. That is useful, but
production agents face a harder state-management problem:

- two agents may hold different values for the same user or organization;
- an explicit correction must supersede stale state without erasing its history;
- inferred context must not outrank direct or authoritative evidence;
- private context must not leak across users, agents, or tenants;
- a retrieved answer needs provenance: what supports it, when was it valid, and what replaced it;
- retries and concurrent workers must not create duplicate writes or multiple winners;
- deletion, revocation, expiration, and delayed indexing must behave predictably.

MemoryOS is the control plane for those transitions. PostgreSQL remains authoritative, claims and
revisions preserve history, policies govern conflicts, and retrieval delivers only context that is
valid and permitted for the current request.

## What MemoryOS Provides

| Capability | Purpose |
| --- | --- |
| Governed ingestion | Extract reusable state while filtering unsafe, low-confidence, or transient content. |
| Claims and revisions | Record attributable changes instead of silently mutating a summary blob. |
| Conflict resolution | Reconcile corrections and contradictions using evidence, recency, and source context. |
| Temporal validity | Distinguish current, historical, future, superseded, and expired state. |
| Provenance | Preserve source events, evidence, versions, and resolution decisions. |
| Scoped coordination | Enforce tenant, user, agent, consent, and category boundaries. |
| Durable idempotency | Make retries and duplicate event delivery converge on one logical outcome. |
| Context retrieval | Combine semantic relevance with governance, lifecycle, and temporal filters. |
| Operational reliability | Use queued extraction, transactional outbox delivery, retries, and dead-letter handling. |
| Domain overlays | Add typed domain state without replacing the governed general engine. |

## Where It Fits

MemoryOS does not replace an agent framework, a model, or a human knowledge base. It sits between
AI applications and their state infrastructure:

The common API remains simple:

```python
from memoryos import Memory

memory = Memory(api_key="...")
memory.add(messages=conversation, external_user_id="alice")
context = memory.get(query="What context matters for this request?", external_user_id="alice")
```

Behind those calls, MemoryOS manages extraction jobs, source events, claim revisions, conflicts,
temporal state, provenance, indexing, and access boundaries.

## Current Scope

The backend includes the general governed-memory engine, conflict and claim ledgers, temporal and
lifecycle handling, provenance-aware retrieval, tenant isolation, consent-aware cross-agent
memory, structured domain paths, asynchronous workers, and internal correctness/regression
benchmarks.

It should not yet be described as proven for unrestricted high-scale traffic. The current focus is
external memory-quality evaluation and a controlled, observable beta launch.

## Quick Start

Prerequisites: Python 3.12+, Docker, and Docker Compose.

```bash
git clone https://github.com/memengine/memory-api
cd memory-api
cp .env.example .env
docker compose up -d postgres redis qdrant localstack
python -m pip install -e ".[dev]"
alembic upgrade head
uvicorn api.main:app --reload --port 8000
```

To exercise queued writes, start a worker in another terminal:

```bash
celery -A api.celery_app.celery_app worker -Q starter-extraction,growth-extraction,scale-extraction,enterprise-extraction,celery,reembedding,dead-letter --loglevel=info
```

The API and OpenAPI documentation are available at `http://localhost:8000` and
`http://localhost:8000/docs`.

## Repository Guide

```text
api/                  FastAPI service, governance engine, workers, and persistence
benchmarks/internal/  Private correctness and regression framework
benchmarks/public/    Isolated adapters for established public benchmarks
docs/                 Design notes, contracts, runbooks, and extraction specification
sdk/python/           Python SDK
sdk/typescript/       TypeScript SDK
scripts/              Operational and benchmark utilities
tests/                Unit, security, integration, and evaluation suites
```

Read [ARCHITECTURE.md](ARCHITECTURE.md) for the system model,
[DOMAIN_SCHEMAS.md](DOMAIN_SCHEMAS.md) for typed overlays, and
[CONTRIBUTING.md](CONTRIBUTING.md) before opening a change.





## License

MIT License. See [LICENSE](LICENSE).
