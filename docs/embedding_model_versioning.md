**Purpose**
This doc explains the embedding model versioning system that was added to prevent a model change from breaking all stored vectors at once.

**What Was Added**
- model registry table: [f2a3b4c5d6e7_add_embedding_model_versioning.py](d:/memoryos/memory-api/api/db/migrations/versions/f2a3b4c5d6e7_add_embedding_model_versioning.py)
- runtime model service: [embedding_service.py](d:/memoryos/memory-api/api/services/embedding_service.py)
- per-model Qdrant collection support: [vector_store.py](d:/memoryos/memory-api/api/db/vector_store.py)
- model-aware retrieval: [retriever.py](d:/memoryos/memory-api/api/services/retriever.py)
- tenant re-embedding task: [reembedding_tasks.py](d:/memoryos/memory-api/api/tasks/reembedding_tasks.py)
- re-embedding monitoring route: [internal.py](d:/memoryos/memory-api/api/routers/internal.py)

**Why This Exists**
Before this change, embeddings were effectively hardcoded to one model and one Qdrant collection.

That meant:
- if the embedding model changed
- or dimensions changed
- or provider changed

then old vectors and new query embeddings could become incompatible.

Now embeddings are treated as versioned data.

**Default Model**
The seeded default model is:

```text
id: gemini-embedding-001-v1
provider: gemini
model_name: gemini-embedding-001
dimensions: 1536
qdrant_collection: memories
is_active: true
```

Important:
- this default points to the existing `memories` collection
- so current production vectors continue to work after the migration

**What Is Stored Now**
Each memory row now has:
- `embedding_model_id`

Each embedding model row stores:
- logical version id
- provider
- provider model name
- vector dimensions
- target Qdrant collection
- whether it is the active model for new writes

**How New Writes Work**
When the system creates a new embedding:
1. it asks `EmbeddingService` for the active model
2. it generates the vector using that model
3. it stores `memories.embedding_model_id`
4. it writes the vector into that model's Qdrant collection

So new writes are always tied to an explicit model version.

**Safe Activation Path**
Do not switch the active model with raw SQL unless you also understand the Redis cache implications.

Preferred operator path:

```text
POST /v1/internal/embedding-models/activate/{model_id}
```

Why:
- it updates the active model in the database
- it invalidates the shared Redis active-model cache
- it rewrites the cache immediately with the new active model

This avoids a temporary split where some replicas still read the old active model from cache.

**How Retrieval Works**
Normal case:
- if a user's memories all belong to one model version, retrieval searches only that version's collection

Migration window case:
- if a user's memories temporarily exist under more than one embedding model version
- retrieval searches both collections
- then merges results before ranking

Important:
- this multi-model search is a migration bridge, not something to keep forever

**How Re-Embedding Works**
Re-embedding is handled by:

```text
api.tasks.reembedding_tasks.reembed_tenant
```

It:
- takes `tenant_id`, `old_model_id`, `new_model_id`
- reads memories still on the old model
- re-embeds them with the new model
- writes vectors to the new collection
- updates `memories.embedding_model_id`
- deletes the old vector from the old collection

It is:
- resumable
- cursor-based
- rate-limited

Redis cursor key format:
```text
reembed:{tenant_id}:{old_model_id}:cursor
```

**Monitoring Endpoint**
Use this endpoint to monitor re-embedding progress:

```text
GET /v1/internal/reembedding-status
```

It reads progress from `backfill_jobs`.

**Safe Migration Flow**
When switching to a new embedding model, use this order:

1. Insert new row in `embedding_models`
2. Keep old model row in place
3. Switch `is_active=true` to the new model for new writes
4. Run tenant re-embedding jobs in the background
5. Monitor with `GET /v1/internal/reembedding-status`
6. Only after all memories are migrated, consider removing the old collection

**Important Rules**
- never delete an old collection before all memories are migrated off it
- never assume all vectors belong to one model forever
- keep `embedding_model_id` and `qdrant_collection` aligned
- treat model switches like data migrations, not simple config changes

**Short Recommendation**
- use `gemini-embedding-001-v1` as the stable baseline
- create a new version row for every future model change
- re-embed gradually per tenant
- monitor status before removing old vector data
