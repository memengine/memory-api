# Local contributor development

Use a fresh clone and synthetic data. This guide is for code contribution, not a
production deployment or a private acceptance run. Never overwrite an existing
`.env` or reuse a cloud/shared-test database to follow it.

Verification note (25 October 2026): the documented starter tests passed and the
contributor Compose configuration parsed. The complete fresh-install,
migration/container/API startup path has not yet been smoke-tested. Please
report setup failures rather than interpreting the commands below as a claim
that this full path passed.

## 1. Install the Python project

Requirements: Git and Python 3.12+. Docker Compose v2 is needed only for service
work. Node.js 20 is used for TypeScript SDK checks.

Fork first if you intend to submit a PR, then clone your fork. A read-only
checkout of upstream also works:

```bash
git clone https://github.com/memengine/memory-api.git
cd memory-api
python -m venv .venv
```

Windows PowerShell (activation is not necessary):

```powershell
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -e '.[dev]'
if (Test-Path -LiteralPath .env) { throw 'Existing .env found. Do not overwrite it.' }
Copy-Item -LiteralPath contributor.env.example -Destination .env
```

macOS/Linux:

```bash
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -e '.[dev]'
test ! -e .env && cp contributor.env.example .env
```

If `.env` already exists, stop and inspect your setup rather than overwriting it.
Dependency installation requires internet access, but the starter tests below
use mocks rather than a paid provider. Do not paste real keys into this profile.
The larger `.env.example` documents optional deployments/integrations; it is not
the minimal contributor configuration.

## 2. Run a no-provider starter check

From the repository root on Windows:

```powershell
.\.venv\Scripts\python.exe -B -m pytest tests/unit/test_sdk_preferences.py tests/unit/test_domain_schema_registry.py -q -p no:cacheprovider
```

On macOS/Linux, use the same arguments with `.venv/bin/python`. These are
focused SDK/mock and schema-registry checks, not full governance qualification.
They do not require running PostgreSQL/Redis or a real model-provider key.

## 3. Optional: local infrastructure and API

Use the separate [compose.contributor.yml](compose.contributor.yml), **not** the
deployment/scale/test overlays. The explicit file avoids loading your ignored
`docker-compose.override.yml`. The fixed project name separates these volumes
and containers from the old default stack. Host ports bind only to loopback:

| Service | Host endpoint |
| --- | --- |
| PostgreSQL (database `memoryos_contributor`) | `127.0.0.1:15432` |
| Redis | `127.0.0.1:16379` |
| Qdrant | `127.0.0.1:16333` |
| LocalStack (local AWS emulation) | `127.0.0.1:14566` |
| API, started separately below | `127.0.0.1:18000` |

No real AWS account or IAM credentials are required. Local credentials in the
template are intentionally public test values; never expose this stack remotely.
Use a fresh shell without exported production settings/credentials: process
environment variables can override `.env` values. Compose syntax inspection does
not start services:

```bash
docker compose --env-file contributor.env.example -f compose.contributor.yml -p memoryos-contributor config --quiet
docker compose --env-file contributor.env.example -f compose.contributor.yml -p memoryos-contributor up -d --wait
```

Run migrations and API on Windows:

```powershell
.\.venv\Scripts\alembic.exe upgrade head
.\.venv\Scripts\python.exe -m uvicorn api.main:app --host 127.0.0.1 --port 18000 --env-file .env
```

macOS/Linux:

```bash
.venv/bin/alembic upgrade head
.venv/bin/python -m uvicorn api.main:app --host 127.0.0.1 --port 18000 --env-file .env
```

Use `http://127.0.0.1:18000/docs` for the API schema and
`http://127.0.0.1:18000/health` for dependency status. Check every dependency field;
HTTP 200 alone is not enough. If migrations fail, preserve the exact error and
commit hash and open a sanitized setup issue. Do not stamp/skip migrations to
make the example appear healthy.

This profile leaves model keys empty, telemetry disabled and experimental flags
off. Health/schema exploration is not a working ingestion demo: authenticated
tenant provisioning, provider-dependent embeddings/extraction and worker setup
require their own explicit configuration. Do not enable flags, add billing/Clerk
keys or run a paid provider merely to contribute docs or tests. LocalStack
emulates services; it is not a substitute for cloud or authorization testing.

Stop only this contributor stack when finished (volumes/data are retained):

```bash
docker compose --env-file contributor.env.example -f compose.contributor.yml -p memoryos-contributor down
```

Do not use `make clean` or `down -v` on an existing workspace; these can erase
data. Do not run Terraform or archived AWS workflows for local setup.

## Test paths

| Change | Starting checks |
| --- | --- |
| Docs/templates | Verify links and commands; `git diff --check` for the edited files |
| Python SDK | `python -B -m pytest tests/unit/test_sdk_preferences.py -q -p no:cacheprovider` |
| TypeScript SDK | From `sdk/typescript`: `npm ci`, `npm run typecheck`, `npm run test:package` |
| Backend | Relevant focused tests first; then applicable unit/security/integration checks |
| CI parity | Read `.github/workflows/ci.yml` for its exact migration, service and FAST commands |

Replace `python` above with your virtualenv executable. The full unit directory
contains more than the starter checks; integration/security suites may require
additional setup. Read each test's prerequisites rather than assuming an entire
directory is offline. TypeScript package checks build package artifacts; do not
commit generated `node_modules` or `dist`.

The CI workflow retains migration, unit, governance integration, FAST regression
and TypeScript package checks. Manual integration benchmarks require local test
services; provider evaluation requires explicit paid-call approval. Private
holdouts and acceptance tooling are not part of contributor setup.

## Troubleshooting

- **Port occupied/forbidden:** identify the listener or OS reservation. Do not
  stop unrelated services or delete volumes. Keep the local endpoints consistent
  if you deliberately change this fresh stack's host ports and `.env` together.
- **Wrong DB/URL:** compare the local template with your process environment;
  never print a real DSN/password into an issue.
- **Unavailable model/provider:** expected with empty keys; use mocked tests.
  Do not silently swap providers or treat missing output as a pass.
- **Windows Celery:** the existing worker topology is separate from this starter
  profile. Discuss a reproducible Linux/container setup for worker-specific work;
  a running API does not prove queued writes are being processed.
- **Need help:** open a question/setup issue with OS, tool versions, commit,
  exact command and sanitized error. No private data or `.env` contents.
