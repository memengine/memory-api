# MemoryOS architecture

Start with the [README](README.md) to integrate your app or the [Docker guide](beta/README.md) to run the service.

## Components

| Component | Job |
| --- | --- |
| FastAPI | Authenticates requests and exposes write, read, and operator endpoints |
| PostgreSQL | Stores records, source events, history, jobs, and pending changes |
| Celery | Processes writes and background jobs |
| Redis | Runs the job broker, cache, and rate limits |
| Qdrant | Indexes records for retrieval |
| Publisher | Sends committed record changes to the search index |

## Ingestion and delivery

Your app sends an update with a user or account ID. MemoryOS creates a job, processes the update, and records its source. It saves accepted changes in PostgreSQL and publishes them to Qdrant. Your app can poll the job and retrieve the current records with their source information.

## Pending service correction

When a correction needs review, retrieval can return a notice for the affected context. An operator selects the current record, reviews the proposed change, and applies or declines it. Apply updates the record and its history and queues publication. See the [operator API](beta/OPERATOR.md).

## Client boundary

Your server supplies the authenticated user or account ID. Keep API keys server-side and check the retrieval notice before using the returned context in an answer or action.
