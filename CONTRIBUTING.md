# Contributing to MemoryOS

MemoryOS is a governed context layer for AI agents. Contributions should strengthen correctness,
traceability, isolation, lifecycle behavior, or developer usability—not only add another way to
store text.

## Useful Contribution Areas

- Claim reconciliation, conflict policy, and revision-chain correctness.
- Provenance, evidence, temporal validity, and lifecycle behavior.
- Tenant, user, agent, and consent isolation.
- Retrieval relevance without weakening governance filters.
- Reliability, idempotency, outbox, retry, and dead-letter behavior.
- SDK contracts, documentation, and reproducible benchmark adapters.
- Domain overlays with a clear need for typed state.

## Development Setup

Prerequisites: Python 3.12+, Docker Desktop or Docker Engine, Docker Compose, and Git.

```bash
git clone https://github.com/YOUR_USERNAME/memory-api
cd memory-api
python -m venv .venv
```

Activate the environment and install dependencies:

```bash
python -m pip install --upgrade pip
python -m pip install -e ".[dev]"
cp .env.example .env
docker compose up -d postgres redis qdrant localstack
alembic upgrade head
```

Run the API:

```bash
uvicorn api.main:app --reload --port 8000
```

Queued extraction and lifecycle work requires an appropriate Celery worker. See `docker-compose.yml`
for the production-like queue layout.

## Tests

Start with focused tests for the code you changed, then run:

```bash
python -m pytest tests/unit tests/security -q
python -m benchmarks.internal.orchestrator --tier fast
```

Integration suites require their declared PostgreSQL, Redis, Celery, or Qdrant services. Provider
evaluations are paid/manual and must never be required for an ordinary pull request.

Never load the internal holdout during development, CI, public benchmark work, or prompt tuning.

## Change Requirements

- Preserve PostgreSQL as the durable authority.
- Add migrations for schema changes, including downgrade behavior.
- Keep retries and duplicate events idempotent.
- Preserve provenance and existing version chains unless the change is an explicit privacy deletion.
- Test cross-tenant and cross-user boundaries for access-related changes.
- Separate benchmark or fixture failures from product failures.
- Do not modify production behavior merely to improve a benchmark score.
- Document externally visible API or lifecycle changes.

## Domain Overlays

Read [DOMAIN_SCHEMAS.md](DOMAIN_SCHEMAS.md) before adding a domain. A domain overlay must add typed
state with a clear product reason, remain fail-open relative to the general engine, and preserve
consent and provenance rules.

## Pull Requests

Use a focused branch and Conventional Commit messages, for example:

- `feat: preserve evidence across claim revisions`
- `fix: prevent duplicate durable event delivery`
- `test: add temporal correction regression scenario`
- `docs: clarify governed context lifecycle`

Before opening a pull request:

1. Run focused tests and the FAST benchmark gate.
2. Confirm `git diff --check` is clean.
3. Include migrations, tests, and documentation required by the change.
4. State whether production behavior changed.
5. State which infrastructure and provider calls were used.
6. Confirm that holdout data was not accessed.

Use GitHub Issues for defects and scoped proposals, and Discussions for architectural questions.
