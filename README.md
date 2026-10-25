# MemoryOS

**Governed context for AI applications: explicit scope, attributable changes, and current preferences.**

[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)
[![CI](https://github.com/memengine/memory-api/actions/workflows/ci.yml/badge.svg)](https://github.com/memengine/memory-api/actions/workflows/ci.yml)
![Python 3.12+](https://img.shields.io/badge/Python-3.12%2B-blue)

[Start contributing](CONTRIBUTING.md) · [Local development](DEVELOPMENT.md) · [Report a bug](https://github.com/memengine/memory-api/issues/new/choose) · [Security](SECURITY.md)

MemoryOS (product name: MemoryO) explores a practical question: **when several
parts of an AI application update a preference, which value should be current,
who supplied it, and did the correction actually apply?**

The customer keeps its existing agent or chat experience. MemoryOS provides
backend APIs and governed context; it does not require a new end-user UI.

## Project status

**Early-stage, maintainer-led development. Contributors are welcome.** This is
not a production-readiness or completed pilot-qualification claim. The current
development focus is application-declared, explicitly scoped preferences. The
repository also contains broader experimental memory paths; their presence is
not a promise that every path is qualified or enabled.

- Local coding-agent examples have consumed a stored project preference and a
  later explicit correction in a fresh session. These are small synthetic
  demonstrations, not customer validation or proof of an advantage over SQL.
- Automatic natural-language interpretation and inferred scope are outside the
  current focus. Experimental flags remain off by default.
- Hosted AWS deployment workflows are archived. No hosted API or AWS account is
  required to contribute documentation, SDK tests, or focused unit tests.
- No production SLA, general prompt-injection protection, autonomous agent
  authorization, or forced downstream model compliance is claimed.

## Why this exists

Saving another vector is not enough when an application needs to distinguish an
old preference from its replacement, handle a retry, or explain a selected value.
MemoryOS investigates those state-management responsibilities behind a common
API rather than leaving each application to assemble them independently.

For example:

1. An application explicitly declares a global programming-example preference.
2. It declares a different preference for one project.
3. A second client corrects that project preference using the observed target
   and version.
4. A later scoped read should select the committed correction without changing
   the global preference.

The application supplies identity, property, value, scope and evidence. A queued
job means **admitted**, not **applied**. Status, the created record, provenance and
the current scoped selection must be correlated before reporting an update as
applied. PostgreSQL is the durable authority; a cache or vector result is not an
authorization decision.

## Start contributing

You do not need an AWS account, a paid model key, or access to private evaluation
data to make a useful contribution.

| If you like… | A useful starting point |
| --- | --- |
| Documentation | Try the setup on your OS; report the exact first failing command. |
| Python | Add a synthetic regression around a public request, scope or replay contract. |
| TypeScript | Improve SDK error handling, package-contract tests or examples. |
| Backend correctness | Propose a small change with an invariant and a reproducible failure. |
| Developer experience | Make an existing API response or local troubleshooting step clearer. |

See the [starter contribution ideas](CONTRIBUTING.md#starter-contribution-ideas)
and [open issues](https://github.com/memengine/memory-api/issues). Those ideas are
suggestions, not preassigned tickets: open or comment on an issue before starting
a larger change. Documentation-only and first-time contributions are welcome.

## Local development

Prerequisites: Git and Python 3.12+. Docker Compose is needed only for the local
service path; Node.js 20 is used by the TypeScript SDK CI job.

Start in a **fresh clone**, not a folder containing production credentials or
private evaluation artifacts:

~~~bash
git clone https://github.com/memengine/memory-api.git
cd memory-api
python -m venv .venv
~~~

Follow [DEVELOPMENT.md](DEVELOPMENT.md) for Windows and macOS/Linux commands,
a no-provider starter test, and an isolated loopback-only service configuration.
The local setup does not enable experimental features or provision cloud resources.
An API health response is not proof that a write or governance journey passed.

## Trust boundary

- Tenant service keys stay on the customer's server. They are tenant-admin
  credentials, not end-user authentication or a promise of per-human isolation
  on every ID/history/status endpoint.
- The customer application authenticates people and authorizes access to owners
  and projects. The model does not decide authority or permissions.
- Preference reads and replacements use explicit owner/scope/target bindings.
  Evidence binds submitted text; it does not establish human authorship or intent.
- A selected preference is context for an agent, not an enforcement mechanism
  controlling the agent's generated code or tool actions.

## Repository map

| Path | Contents |
| --- | --- |
| [api/](api/) | FastAPI routes, schemas, services, workers and persistence |
| [sdk/python/](sdk/python/) | Python client and its own installation guide |
| [sdk/typescript/](sdk/typescript/) | TypeScript client and package-contract tests |
| [tests/](tests/) | Unit, security and service-dependent integration tests |
| [benchmarks/internal/](benchmarks/internal/) | Development regression tooling; private holdouts are not contributor input |
| [benchmarks/public/](benchmarks/public/) | Public benchmark adapters with separate setup requirements |
| [docs/](docs/) | Design documents, API contracts and operational notes |
| [infrastructure/](infrastructure/) | Deployment source; not part of ordinary contributor setup |

Read [ARCHITECTURE.md](ARCHITECTURE.md) for implementation context. Older design
documents may describe experimental or deferred capabilities; current limitations
above still apply. See [workflow guidance](.github/WORKFLOWS.md) before running a
manual benchmark or restoring a deployment workflow.

## Community and security

Use [GitHub Issues](https://github.com/memengine/memory-api/issues/new/choose)
for public bugs, questions and scoped proposals. Follow the
[Code of Conduct](CODE_OF_CONDUCT.md). Report suspected vulnerabilities privately
using [SECURITY.md](SECURITY.md), not in public logs or issues.

Never submit real customer conversations, credentials, database dumps, private
acceptance cases or holdout answers. Use minimal synthetic reproductions.

## License

[MIT](LICENSE). Contributions are made under the repository's existing licence.
