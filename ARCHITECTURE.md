# MemoryOS architecture

This describes the current local beta's main paths. It is not a guarantee that
every experimental route is enabled or qualified. Use [the beta guide](beta/README.md)
for the current runtime setup.

## Components

| Component | Responsibility |
| --- | --- |
| FastAPI | Credential checks, tenant/owner context, admission, reads and operator routes |
| PostgreSQL | Durable jobs/receipts, records, source events, claim revisions, history and outbox |
| Celery workers | Asynchronous extraction, reconciliation and background processing |
| Redis | Broker, cache, rate limits and coordination; not the authoritative replay receipt |
| Qdrant | Derived semantic index synchronized through the outbox |
| Scoped beta publisher | Dispatches the selected tenant's vector outbox; not full Beat scheduling |

The beta Compose profile runs its own storage/network. LocalStack supplies local
AWS-compatible endpoints; no real AWS account is required. The app DB role is
non-superuser. These choices do not make the public test credentials suitable for
hosting.

## Ingestion and delivery

```text
Application-authenticated owner + registered service credential + declared scope
  -> API validation / quota / durable admission
  -> source event and job
  -> worker extraction and reconciliation
  -> accepted state transition OR retained pending correction
  -> SQL history / claim revision / transactional vector outbox
  -> scoped publisher and worker synchronize Qdrant
  -> governed retrieval + provenance + visibility status
  -> application checks visibility before constructing an answer
```

Natural-language extraction/relation handling uses models; it is distinct from
an explicit typed operation. Admitted work is not applied state, and a completed
job does not alone prove the intended correction occurred. Vector publication
follows SQL commit. Current SQL and authorization checks remain necessary around
derived/cached results; no universal lag/availability guarantee is implied.

## Pending service correction

A retained unresolved correction can withhold the affected category/scope from
current context. An operator discovers pending items, selects an explicit current
target, reviews a version-bound preview and submits apply or decline. Current
permissions, source policy and target bindings are rechecked; apply commits the
state/history/outbox/audit change atomically. Publication is subsequent.

Use [beta/OPERATOR.md](beta/OPERATOR.md) for exact HTTP contracts, replay and token
limits. This admin-key interface is separate from dashboard-authenticated writer
management and from disabled preference-confirmation/interpretation paths.

## Client boundary

API keys are tenant-scoped with separate permissions. The application supplies
trusted user identity and authorizes which owners/projects a caller may access.
Source evidence supports attribution, not real-world truth or human authorship.
SDKs transport visibility; they cannot force applications or models to honor it.
Historical/domain/consent code outside this beta path is reference material, not
additional release qualification.
