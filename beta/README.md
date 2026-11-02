# MemoryO local developer beta

This package is for a **guided, synthetic-only local beta**, not production.
It contains the source for the bounded service-fact governance journey and a small
HTTP client. It does not contain Relay, private acceptance cases, previous research
reports, real credentials, databases, generated test output or a trained model.

## What is verified

The preceding local candidate demonstrated source-priority updates, pending
containment/visibility, explicit operator resolution, vector publication and exact
replay in bounded synthetic journeys. Three Relay roles subsequently passed an
**exact quotation path** with separate citations. General model answer grounding,
arbitrary canonical matching, exhaustive security/load behavior, production
readiness and a technical advantage over competent SQL are not established.

`CANDIDATE.json` identifies that runtime and the equivalent source files. Git may
normalize line endings; the source hashes explicitly normalize CRLF to LF. This
is a source package, not a claim that a newly built image has identical dependency
bytes to the earlier runtime.

**Packaging checks are offline/static. The fresh Compose install, database init,
bootstrap and paid example below have not been executed as part of packaging.**
The existing installed runtime's previous results do not prove this fresh-install
path. Preserve the first setup failure and report it; do not bypass migration,
ownership or permission guards. Internet is needed to build/install dependencies.

## Fresh local startup

Requires Docker Compose v2 and Python 3.12+ for the host-side example. Use a fresh
checkout/extracted source package and a clean shell. Do not use the root deployment
Compose files or overwrite an existing environment file.

```powershell
# Run from the source-package root. Refuse to overwrite existing beta credentials.
$betaContainers = docker ps -aq --filter label=com.docker.compose.project=memoryos-beta
$betaVolumes = docker volume ls -q --filter label=com.docker.compose.project=memoryos-beta
if ($betaContainers -or $betaVolumes) { throw 'Existing beta resources: stop and inspect; do not reuse or delete them for a fresh install.' }
if (Test-Path beta/beta.env) { throw 'Existing beta.env: inspect it before proceeding.' }
Copy-Item beta/beta.env.example beta/beta.env
docker compose --env-file beta/beta.env -f beta/compose.yml -p memoryos-beta build api
docker compose --env-file beta/beta.env -f beta/compose.yml -p memoryos-beta up -d --wait postgres redis
docker compose --env-file beta/beta.env -f beta/compose.yml -p memoryos-beta up -d qdrant localstack
# Wait for Qdrant/LocalStack readiness; diagnose failures instead of skipping dependencies.
docker compose --env-file beta/beta.env -f beta/compose.yml -p memoryos-beta run --rm api alembic upgrade head
docker compose --env-file beta/beta.env -f beta/compose.yml -p memoryos-beta up -d api worker background
docker compose --env-file beta/beta.env -f beta/compose.yml -p memoryos-beta exec api python beta/bootstrap.py --create
# This file contains the newly generated local operator secret: keep it private.
docker compose --env-file beta/beta.env -f beta/compose.yml -p memoryos-beta cp api:/tmp/beta-credentials.local.json beta/credentials.local.json
```

The profile uses dedicated project volumes/network, a non-superuser runtime DB
role and no shared host DB/broker. Only API `127.0.0.1:18000` is exposed. Check
`/health` dependency details and `/docs`; HTTP 200 alone is not complete readiness.
If the host port is occupied, do not stop another service to make this work.
Change this profile's host mapping and the example's `--base-url` together.

After bootstrap, put the printed **new tenant ID** in `LOCAL_VECTOR_TENANT_ID` in
`beta/beta.env`, then start its single-purpose publisher:

```powershell
docker compose --env-file beta/beta.env -f beta/compose.yml -p memoryos-beta --profile publisher up -d publisher
```

This dispatches only that tenant's vector outbox, not full Beat/lifecycle schedules.
Keep V2/experimental flags off. No real AWS account, Clerk or dashboard is required
for this API-key workflow. Local-only example passwords are not hosted credentials.
The default provider key is empty; setup does not authorize provider usage.

## One two-service integration example

After explicitly approving your own extraction/embedding spend, place your own
OpenAI key in the ignored `beta/beta.env` and recreate only this beta's app roles
using the same explicit Compose command. Do not copy a real project environment.
Do not share that file or include it in a bug report.

```powershell
python beta/example.py demo --allow-paid-calls --output beta/journey.local.json
```

The example creates two new scoped read/write client keys and credential-bound
Billing20/CRM95 writers, a fresh synthetic owner and an application-declared
global/account scope. It submits Growth, INR, Scale and Enterprise source events,
polling each original job without resubmitting on automatic retry. It then reads
current context and discovers pending corrections. Credentials remain in memory;
the output does not include newly created raw client keys.

This optional ingestion path uses existing model extraction/relation handling,
not guaranteed natural-language interpretation. **Enterprise may apply or remain
pending; the example does not force a pending fixture or silently resolve it.**
Other failures remain failures. An admitted/completed job is not proof of the
desired memory transition; inspect stored content, provenance and current results.

The final adapter returns exact record quotations/IDs and refuses a visibility
hold. It is not a general agent, relevance/entailment validator or model answer.
Applications must supply stable owner/scope/authorization, honor holds before
inference/actions, and own their business-field identity and answer policy.

## Pending correction: explicit operator choice

Use the returned candidate ID and explicitly choose its current target from
authorized listing/history. Do not infer the target automatically by text/order.

```powershell
python beta/example.py preview --owner OWNER --candidate CANDIDATE_UUID --target TARGET_UUID --output beta/preview.local.json
# Review the actual preview, priorities, target, scope and blockers first.
python beta/example.py decide --owner OWNER --preview-file beta/preview.local.json --action apply --allow-paid-calls
# Or explicitly decline without changing current facts:
# python beta/example.py decide --owner OWNER --preview-file beta/preview.local.json --action decline
python beta/example.py read --owner OWNER --allow-paid-calls
```

Preview is read-only. Apply can call the embedding provider and atomically commits
SQL history/winner/outbox/audit/candidate closure; publication is subsequent. Exact
replay returns the original receipt. Current-state preview equality can become
valid again after identical policy restoration; it is not permanent revocation.
See [the operator contract](OPERATOR.md) for all limits.

## Feedback and stopping

Record setup time separately from integration time, custom code/workarounds,
first failure, request/job IDs, supported vs expected behavior, and the work this
service saved or added compared with your own SQL/CAS/audit service. No willingness
to adopt/pay is inferred from passing a demo. Stop on a scope/authorization failure
or if continuing requires a backend workaround; preserve evidence instead of
retrying away a failure. No private acceptance suite is part of this beta.

Stop only this fresh beta stack, retaining its volumes:

```powershell
docker compose --env-file beta/beta.env -f beta/compose.yml -p memoryos-beta down
```

Do not use `down -v`, clear someone else's queues, or change existing application
data. No deployment, push, paid run or new service startup was part of packaging.
