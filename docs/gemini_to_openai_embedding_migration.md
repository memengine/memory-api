**Purpose**
This runbook explains how to migrate embeddings from Gemini to OpenAI without taking the API down.

It also separates:
- what **you** must do manually
- what the **system** does automatically

**Goal**
Move from:
- current active model: `gemini-embedding-001-v1`

to something like:
- new model: `text-embedding-3-small-v1`

without breaking:
- stored vectors
- retrieval
- live tenant traffic

**Important Rule**
Do not replace the old model in place.

Always:
1. add a new model row
2. switch active model
3. re-embed in background
4. remove old collection only at the very end

---

**Phase 1 — Prepare The New Model**

**You run**
- add the new provider secret/config
- insert the new embedding model row in PostgreSQL

Example model row:

```sql
INSERT INTO embedding_models (
  id,
  provider,
  model_name,
  dimensions,
  qdrant_collection,
  is_active
)
VALUES (
  'text-embedding-3-small-v1',
  'openai',
  'text-embedding-3-small',
  1536,
  'memories_openai_v1',
  FALSE
);
```

**You also do**
- add the OpenAI key in your real env/secrets
- restart/redeploy the API if needed so the key is available

**System does internally**
- nothing switches yet
- Gemini remains active
- all writes and retrieval continue normally

**Expected result**
- new model exists in DB
- old Gemini model is still active
- no tenant-facing behavior changes yet

---

**Phase 2 — Switch Active Model**

**You run**
- the internal activation endpoint

Preferred operator call:

```text
POST /v1/internal/embedding-models/activate/text-embedding-3-small-v1
```

If you must use SQL for emergency/manual work, remember:
- the Redis key `embedding_models:active` must also be invalidated
- otherwise replicas may continue using the previous active model for up to 5 minutes

**System does internally**
- `EmbeddingService.get_active_model()` starts returning the OpenAI model
- invalidates and refreshes the shared active-model cache in Redis
- new embeddings use OpenAI
- new writes go to the new Qdrant collection
- old Gemini vectors still remain untouched

**Expected result**
- new writes use OpenAI
- old memories still point to Gemini vectors
- API stays up

---

**Phase 3 — Background Re-Embedding**

**You run**
- the re-embedding task for each tenant you want to migrate

Current task:

```text
api.tasks.reembedding_tasks.reembed_tenant
```

Current Python trigger example:

```powershell
osenv\Scripts\python -c "from api.tasks.reembedding_tasks import reembed_tenant; result = reembed_tenant.delay('TENANT_UUID', 'gemini-embedding-001-v1', 'text-embedding-3-small-v1', batch_size=50); print(result.id)"
```

If running directly for a local/manual one-off:

```powershell
osenv\Scripts\python -c "from api.tasks.reembedding_tasks import run_reembedding_cycle; print(run_reembedding_cycle('TENANT_UUID', 'gemini-embedding-001-v1', 'text-embedding-3-small-v1', batch_size=50))"
```

**System does internally**
- reads memories still on the old model
- embeds them with the new model
- writes vectors into the new OpenAI Qdrant collection
- updates `memories.embedding_model_id`
- deletes old vector from old Gemini collection
- stores progress in Redis cursor:

```text
reembed:{tenant_id}:{old_model_id}:cursor
```

- stores progress rows in `backfill_jobs`

**Expected result**
- migration happens gradually
- API stays online
- no “all tenants break at once” cutover

---

**Phase 4 — Monitor Progress**

**You run**
- monitor status endpoint

```text
GET /v1/internal/reembedding-status
```

**System does internally**
- exposes progress from `backfill_jobs`

What you should look for:
- `status`
- `processed_rows`
- `total_rows`
- `pct_complete`
- `eta_seconds`

**Expected result**
- task moves toward `status = complete`

---

**Phase 5 — Mixed-Version Retrieval Window**

**You do not run anything special**

**System does internally**
- if a user/tenant has memories in both old and new embedding versions
- retrieval searches both collections
- results are merged before ranking

This is the safety bridge that keeps retrieval working during migration.

**Expected result**
- no outage during migration window
- old and new vectors can coexist temporarily

---

**Phase 6 — Final Verification**

**You run**
- DB check to confirm no memories still use the old model

Example:

```sql
SELECT COUNT(*)
FROM memories
WHERE embedding_model_id = 'gemini-embedding-001-v1';
```

Expected:

```text
0
```

Optional collection check:
- confirm new OpenAI collection has the expected point count
- confirm old Gemini collection is no longer needed

---

**Phase 7 — Retire The Old Collection**

**You run**
- only after all rows are migrated
- remove or archive the old Gemini collection

Important:
- this is a manual ops decision
- do not do it early

**System does internally**
- nothing automatic here
- deletion of old collection is intentionally not automatic

---

**Manual vs Automatic Summary**

**You run manually**
- add new model row
- add new provider key/config
- switch active model
- trigger re-embedding jobs
- monitor status
- verify old model count reaches zero
- remove old collection at the very end

**System handles internally**
- active model lookup
- new-write routing to correct model/collection
- per-memory `embedding_model_id` tracking
- mixed-version retrieval during migration
- re-embedding cursor resume
- progress tracking

---

**Recommended Safe Order**
1. Add OpenAI key/config
2. Insert new `embedding_models` row
3. Switch `is_active`
4. Trigger re-embedding per tenant
5. Monitor `/v1/internal/reembedding-status`
6. Verify old-model row count is `0`
7. Remove old Gemini collection last

---

**What Not To Do**
- do not overwrite the old Gemini model row with OpenAI values
- do not reuse the same Qdrant collection for incompatible model versions
- do not delete the old collection before re-embedding completes
- do not assume switching the active model migrates historical data automatically

---

**Short Recommendation**
- treat embedding model changes like data migrations
- not like simple config flips
- switch active model first for new writes
- migrate old vectors gradually in background
- delete old vector data only after verification

















one confusion is tht if we change  embdeding model then its store or another qdrant collection?
second is if it store on another then we need to  manuly create collection through qdrant website or internally  done?



Yes, it stores in another Qdrant collection.

Current design:

each embedding model row has its own qdrant_collection
so when you change to a new embedding model version, new vectors go to that new collection
old vectors stay in the old collection until re-embedding finishes
Example:

gemini-embedding-001-v1 -> memories
gemini-embedding-001-v2 -> memories_v2_verify
So the answer to your first question is:

yes, new model = separate collection
For your second question:

you do not need to manually create the collection in the Qdrant UI
It is handled internally.

What happens:

when the app first writes/searches against that model’s collection
vector_store.py checks whether the collection exists
if not, it creates it automatically with the configured dimensions
then it creates the needed payload indexes too
So:

manual collection creation is not required
the app creates it automatically
What you do manually:

insert the new row in embedding_models
or use the activation endpoint when switching active model
What the system does internally:

create the new Qdrant collection if missing
write new vectors there
search both old and new collections during migration
re-embed old data into the new collection
Short answer:

new embedding model -> new Qdrant collection
collection creation -> automatic, not manual

