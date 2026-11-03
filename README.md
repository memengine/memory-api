# MemoryOS

**Governed memory for AI applications: source attribution, scoped updates and accountable corrections.**

[![CI](https://github.com/memengine/memory-api/actions/workflows/ci.yml/badge.svg)](https://github.com/memengine/memory-api/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

MemoryOS (product name: MemoryO) provides a backend for applications whose clients
or services write changing context. It records sources and revisions, applies
configured authority rules, and exposes pending corrections for explicit review.
Your application keeps its agent/chat interface and controls authentication,
identity, scope and whether retrieved context is safe to use.

[Run locally](beta/README.md) · [Contribute](CONTRIBUTING.md) · [Security](SECURITY.md)

## Project status

**Local developer beta.** Fresh isolated build, migrations, synthetic bootstrap,
API authentication, workers and the scoped publisher have been checked. Bounded
synthetic governance journeys have exercised source updates, pending visibility,
operator correction and quotation with provenance. These are different pieces of
evidence, not a complete fresh-install-to-agent acceptance result.

The paid ingestion journey has not been rerun after the packaging repair. There
is no production-readiness, general answer-grounding or customer-adoption claim.
Use the setup and operator guides below for the applicable behavior and checks.

## What you can use

| Capability | Integration boundary |
| --- | --- |
| Asynchronous ingestion and status | Submit once, retain the job ID, and distinguish admission from applied state. |
| Credential-bound service sources | Use registered writers and configured authority; do not trust a model's asserted role. |
| Stored records, provenance and history | Inspect where a fact came from and how it changed. |
| Pending correction visibility | Check retrieval visibility before answering; unresolved context may be withheld. |
| Explicit service correction | An operator chooses a target, reviews a version-bound preview and applies or declines through HTTP. |
| Python and TypeScript SDKs | Published clients preserve visibility holds and degraded status; they do not start the backend. |

PostgreSQL is the durable authority. Qdrant is a derived index; Redis supplies
cache and broker state. This is a packaged service, not a demonstrated technical
advantage over a competent SQL/CAS/audit implementation.

## Run locally

Requires Git and Docker Desktop/Compose v2. Python 3.12+ is needed for the host
example; Node.js 20 is used for TypeScript contribution checks.

```bash
git clone https://github.com/memengine/memory-api.git
cd memory-api
```

Follow **[beta/README.md](beta/README.md)** for the verified isolated setup,
synthetic credentials and shutdown commands. Start from a fresh clone and clean
shell. No ZIP, AWS account or hosted backend is required. Setup keys are empty;
model-powered ingestion/retrieval requires your own provider configuration and
explicit spending approval. Keep the local profile on loopback.

To work on code or documentation without providers, use
[DEVELOPMENT.md](DEVELOPMENT.md).

## Install a client

| Language | Published version | Guide |
| --- | --- | --- |
| Python | `pip install memoryo-sdk==0.1.2` | [Python SDK](sdk/python/README.md) |
| TypeScript | `npm install memoryo-sdk@0.1.3` | [TypeScript SDK](sdk/typescript/README.md) |

Set the backend URL explicitly to `http://127.0.0.1:18000` for this beta. The SDKs'
hosted default is not evidence that a hosted service is available. Keep keys on
your server. Before using context, check `memory_visibility` (Python) or
`memoryVisibility` (TypeScript); HTTP 200, a nonempty prompt or `hasContext` alone
is insufficient. Service-correction review remains an
[operator HTTP workflow](beta/OPERATOR.md), not an SDK convenience method.

## Trust boundary

- API keys are tenant-scoped and permission-limited. The bootstrap's operator
  key has admin permissions; its Billing/CRM keys have only read/write permissions.
- A tenant-service key is not an end-user login. Your server must authorize
  owner/project access, including routes without an owner selector.
- Authority comes from configured writers and current backend checks. Evidence
  does not prove human authorship, intent or real-world truth.
- A hold must affect your application's answer/action policy. MemoryOS cannot
  force a model or tool to obey it, or erase context already copied by clients.

## Repository guide

| Path | Purpose |
| --- | --- |
| [beta/](beta/) | Local setup, synthetic bootstrap, HTTP example and operator guide |
| [api/](api/) | Backend routes, workers and persistence |
| [sdk/](sdk/) | Python and TypeScript clients |
| [tests/](tests/) | Mock, unit, security and service-dependent checks |
| [ARCHITECTURE.md](ARCHITECTURE.md) | Current beta components and state transitions |
| [ROADMAP.md](ROADMAP.md) | Bounded next delivery steps |
| [docs/](docs/) | Detailed contracts and historical design material; experimental paths are not release promises |

[GitHub Issues](https://github.com/memengine/memory-api/issues/new/choose) are for
sanitized bugs, questions and proposals. Security concerns go through
[SECURITY.md](SECURITY.md). Follow the [Code of Conduct](CODE_OF_CONDUCT.md).
Contributions use the existing [MIT license](LICENSE).
