# Local contributor development

Use the [Docker guide](beta/README.md) to run the API. Follow the steps below to install development tools and run tests.

## Install for code changes

Requirements: Python 3.12+ and Git. Node.js 20 is used for TypeScript SDK checks.
Docker is needed only for service-dependent work. Start in a clean shell without
exported production credentials; process settings can override environment files.

```bash
git clone https://github.com/memengine/memory-api.git
cd memory-api
python -m venv .venv
```

Windows PowerShell:

```powershell
.\.venv\Scripts\python.exe -m pip install -e '.[dev]'
if (Test-Path -LiteralPath .env) { throw 'Existing .env found; inspect it instead of overwriting.' }
Copy-Item -LiteralPath contributor.env.example -Destination .env
.\.venv\Scripts\python.exe -B -m pytest tests/unit/test_sdk_preferences.py tests/unit/test_domain_schema_registry.py -q -p no:cacheprovider
```

macOS/Linux:

```bash
.venv/bin/python -m pip install -e '.[dev]'
test ! -e .env || { echo 'Existing .env found; stop and inspect it.'; exit 1; }
cp contributor.env.example .env
.venv/bin/python -B -m pytest tests/unit/test_sdk_preferences.py tests/unit/test_domain_schema_registry.py -q -p no:cacheprovider
```

Dependency installation needs internet. These starter checks use mocks and need
no provider key or running storage. They are not end-to-end governance tests.
Do not copy production settings or add model keys just to run mocked tests.

## Test paths

| Change | Relevant checks |
| --- | --- |
| Docs/templates | Check links and command paths; `git diff --check` |
| Python SDK | Root virtualenv: `python -m pytest tests/unit/test_sdk_memory_visibility.py tests/unit/test_sdk_preferences.py -q` |
| TypeScript SDK | In `sdk/typescript`: `npm ci`, `npm run typecheck`, `npm run test:package` |
| Backend | Focused regressions for the changed invariant; service-dependent checks need disposable resources |
| CI parity | Read [.github/workflows/ci.yml](.github/workflows/ci.yml) for exact commands |

Replace `python` with your virtualenv executable. The whole test tree is not an
offline suite. Read prerequisites before running integration, provider or load
checks. Keep generated packages, `dist`, `node_modules` and runtime evidence out
of a contribution. SDK package verification is distinct from backend integration.

## Service-dependent work

Use the [Docker guide](beta/README.md) to run the API and workers. The image contains the source, so rebuild your local image after backend changes. `compose.contributor.yml` provides storage services for development. Use a separate Compose project for each setup.

## Troubleshooting

- **Port occupied:** inspect the listener/reservation. Do not stop someone else's
  service or clear volumes. Keep your own profile's URL and host mapping aligned.
- **Migration failure:** preserve the first error and commit. Do not stamp/skip a
  migration to obtain a healthy response.
- **Wrong resource:** compare settings locally; never print real DSNs or keys.
- **Provider unavailable:** expected with empty keys. Use mock checks; do not
  change providers or enable experimental flags to hide a failure.
- **Windows async checks hang:** restricted execution environments can block
  asyncio's local socketpair. Verify local socket permission before changing SDK
  behavior. Docker workers avoid relying on native Windows Celery support.

Open a sanitized [setup question](https://github.com/memengine/memory-api/issues/new/choose)
with OS/tool versions, commit, exact command and first error. Keep data/volumes
unless you deliberately choose to delete your own disposable resources. Never
run `down -v`, `make clean` or queue-clearing commands on shared resources.
