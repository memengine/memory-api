# Run MemoryOS locally

Use this guide to start MemoryOS with Docker and connect your first app. You'll need Git, Docker with Compose, and Python 3.12 or newer for the command-line example.

## Fresh local startup

From the root of a fresh clone, check that port `18000` and the `memoryos-beta` Docker project name are available. For a second installation, use a different name after `-p`, change the host port in `beta/compose.yml`, and use that port in your API URL.

Create `beta/beta.env` from `beta/beta.env.example`. Keep your existing file if you have one. To save memories in the examples below, set `OPENAI_API_KEY` in this file before starting the API and workers. The examples use your OpenAI account for extraction and embeddings.

Define a short Compose command for your shell.

**PowerShell**

```powershell
function dc { docker compose --env-file beta/beta.env -f beta/compose.yml -p memoryos-beta @args }
```

**Bash**

```bash
dc() { docker compose --env-file beta/beta.env -f beta/compose.yml -p memoryos-beta "$@"; }
```

Start the dependencies.

```text
dc build api
dc up -d --wait postgres redis
dc up -d qdrant localstack
dc logs qdrant localstack
```

Once Qdrant and LocalStack are ready, run the database migrations, start the API and create a test workspace.

```text
dc run --rm api alembic upgrade head
dc up -d api worker background
dc exec api python beta/bootstrap.py --create
dc cp api:/tmp/beta-credentials.local.json beta/credentials.local.json
```

Copy the printed tenant ID into `LOCAL_VECTOR_TENANT_ID` in `beta/beta.env`, then start the publisher.

```text
dc --profile publisher up -d publisher
```

Open [API health](http://127.0.0.1:18000/health) to check PostgreSQL, Redis and Qdrant. The [interactive API reference](http://127.0.0.1:18000/docs) runs on the same port. Keep the generated credentials file on your server. It contains an operator key and separate Billing and CRM keys for the examples.

You're ready to [save and read your first memory](../README.md#2-save-and-read-a-memory) with Python or Node.js.

## One two-service integration example

From the repository root, run this example to see how Billing and CRM updates affect the same account. The flag enables the configured provider calls.

```text
python beta/example.py demo --allow-paid-calls --output beta/journey.local.json
```

Open `beta/journey.local.json` to see the account ID, job results, current records, and any corrections waiting for review. The script submits each update once and polls its job until processing finishes. Use a new output filename for another run.

## Review a correction

If a correction is pending, use its candidate ID and the current memory ID for the same fact. Inspect the preview before choosing an action.

```text
python beta/example.py preview --owner OWNER --candidate CANDIDATE_UUID --target TARGET_UUID --output beta/preview.local.json
python beta/example.py decide --owner OWNER --preview-file beta/preview.local.json --action apply --allow-paid-calls
python beta/example.py read --owner OWNER --allow-paid-calls
```

Use `--action decline` to close the candidate and keep the current record. For request fields and response examples, see the [operator API](OPERATOR.md).

## Stop MemoryOS

Stop this Docker project and keep its data volumes.

```text
dc --profile publisher down
```
