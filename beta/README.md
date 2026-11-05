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
dc --profile publisher ps -a
```

Check that `api`, `worker`, `background`, and `publisher` are all running. If a service exited, use `dc --profile publisher logs SERVICE` to see its error. Open [API health](http://127.0.0.1:18000/health) to check PostgreSQL, Redis and Qdrant. The [interactive API reference](http://127.0.0.1:18000/docs) runs on the same port. Keep the generated credentials file on your server. It contains an operator key and separate Billing and CRM keys for the examples.

You're ready to [save and read your first memory](../README.md#2-save-and-read-a-memory) with Python or Node.js.

## One two-service integration example

From the repository root, run this example to see how Billing and CRM updates affect the same account. The flag enables the configured provider calls. If you already have a completed `beta/journey.local.json`, use it for the review below. Choose a new output filename before starting another demo.

```text
python beta/example.py demo --allow-paid-calls --output beta/journey.local.json
```

Open `beta/journey.local.json` to see the account ID, job results, retrieval response, and any corrections waiting for review. The script submits each update once and polls its job until processing finishes. If retrieval reports that context is unavailable, your agent should withhold the affected answer.

## Review a correction

If `pending_corrections` is empty, skip the review. Otherwise, use `owner` from the report and the `id` of the correction you want to review. For an Enterprise correction that replaces Scale, look for the Scale memory ID in the third job's `created_memory_ids`. Verify that the preview shows Scale as the target and Enterprise as the candidate for the same account.

In PowerShell, load the IDs from your run and choose the target explicitly.

```powershell
$journey = Get-Content beta/journey.local.json -Raw | ConvertFrom-Json
if (@($journey.pending_corrections).Count -eq 0) { throw 'There is no correction to review.' }
$owner = $journey.owner
$journey.pending_corrections | Format-List id, content
$journey.jobs[2].created_memory_ids
$candidate = Read-Host 'Enter the candidate ID for the Enterprise correction'
$target = Read-Host 'Enter the Scale memory ID'
$preview = "beta/preview-$([guid]::NewGuid()).local.json"
python beta/example.py preview --owner $owner --candidate $candidate --target $target --output $preview
if ($LASTEXITCODE -ne 0) { throw 'Preview failed. Stop and check the IDs.' }
(Get-Content $preview -Raw | ConvertFrom-Json).data |
    Format-List scope, target_content, candidate_content, actions, apply_blockers
```

If you confirm this is the intended correction, `apply` is listed, and there are no apply blockers, run these commands in the same terminal. Applying and retrieving can use paid embeddings.

```powershell
python beta/example.py decide --owner $owner --preview-file $preview --action apply --allow-paid-calls
if ($LASTEXITCODE -ne 0) { throw 'Apply failed. Check the response before continuing.' }
python beta/example.py read --owner $owner --allow-paid-calls
```

For this correction, look for Enterprise from CRM and INR from Billing, each with its own source event ID. If indexing is still processing, wait before trying the read again. Use `--action decline` in place of `--action apply` to close the candidate and keep the current record. For request fields and response examples, see the [operator API](OPERATOR.md).

## Stop MemoryOS

Stop this Docker project and keep its data volumes.

```text
dc --profile publisher down
```
