# Domain Schemas and Governed State Overlays

## Why Domain Overlays Exist

Domain overlays are not separate memory databases and are not the primary value proposition of
MemoryOS. They add typed state and domain policy where a generic proposition is insufficient for a
real workflow. The governed general engine remains responsible for provenance, claims, conflicts,
versions, temporal validity, access boundaries, and retrieval.

For example, the EdTech overlay can represent learning stage, weak topics, exam goals, and
forgetting state in a structured profile. Those fields still enter the same governance model: a
new observation needs a source, a conflicting update needs a decision, and cross-agent projection
requires consent.

Use a domain overlay only when typed state changes product behavior. Do not create one merely to
rename generic facts for an industry.

## Current Architecture

MemoryOS has two plugin layers:

| Layer | Purpose | Registry |
| --- | --- | --- |
| Conflict routing | Decides whether unresolved conflicts should go to user clarification or tenant review. | `api/services/conflict_routing/registry.py` |
| Domain schema overlay | Adds structured extraction and domain-aware retrieval context on top of the generic engine. | `api/services/domain_schemas/registry.py` |

The generic engine always stays on. A domain schema should add useful structured state, not replace the base memory system.

Domain schemas can also project safe portable summaries into `universal_memories` for cross-agent sharing. The full domain state stays in its domain table. See `docs/domain_universal_projection.md` for the consent-aware projection rules.

## Available Schemas

| Domain | Status | Main Files | Notes |
| --- | --- | --- | --- |
| EdTech | Available | `api/services/edtech/`, `api/services/domain_schemas/edtech.py` | Structured student profile, forgetting curve, EdTech retrieval context. |
| Healthcare | Routing only | `api/services/conflict_routing/healthcare_router.py` | Conflict routing exists; structured extraction overlay not built yet. |
| HR Tech | Routing only | `api/services/conflict_routing/hrtech_router.py` | Conflict routing exists; structured extraction overlay not built yet. |
| AgriTech | Planned | - | Good community contribution candidate. |
| Customer Support | Planned | - | Good community contribution candidate. |

## How Runtime Selection Works

A tenant enables a schema by setting tenant metadata:

```json
{
  "domain_schema": "edtech",
  "edtech_schema_enabled": true
}
```

For EdTech, this can be enabled with:

```http
POST /v1/tenant/settings/enable-edtech-schema
```

After that:

- `POST /v1/memories/add` still runs the generic extraction pipeline.
- The generic `ConflictResolver` receives `domain_schema` and uses the domain conflict router.
- The domain overlay runs after generic storage and writes structured data, such as `edtech_memories`.
- `POST /v1/memories/retrieve` still returns generic memories, but prepends domain-aware context when available.

## How to Build a Domain Schema

### Step 1: Create the Conflict Router

Copy an existing router:

```text
api/services/conflict_routing/edtech_router.py
```

Rename it for your domain, for example:

```text
api/services/conflict_routing/agritech_router.py
```

Implement:

```python
class AgriTechEntityRouter(BaseEntityRouter):
    def get_domain(self) -> str:
        return "agritech"

    def classify(self, entity_type, memory_a, memory_b):
        if entity_type in self.USER_SESSION_ENTITIES:
            return "user_session"
        if entity_type in self.TENANT_REVIEW_ENTITIES:
            return "tenant_review"
        return None
```

Then register it in:

```text
api/services/conflict_routing/registry.py
```

If your router returns `None`, MemoryOS falls back to `GenericEntityRouter`, so unknown entities still behave safely.

### Step 2: Design the Structured Table

Create a migration for your domain table:

```text
api/db/migrations/versions/add_<domain>_memories.py
```

Use one row per `proxy_user_id + tenant_id` unless your domain truly needs many rows per user. EdTech uses this model:

```text
edtech_memories
  proxy_user_id
  tenant_id
  structured JSONB fields
  last_extraction_at
  extraction_source_job_ids
```

Then add the SQLAlchemy model in:

```text
api/db/models.py
```

### Step 3: Create the Domain Service Folder

Create:

```text
api/services/<domain>/
  __init__.py
  prompt_builder.py
  <domain>_extractor.py
  <domain>_retriever.py
```

Use EdTech as the reference:

```text
api/services/edtech/prompt_builder.py
api/services/edtech/edtech_extractor.py
api/services/edtech/edtech_retriever.py
```

The extractor should expose a method similar to:

```python
extract_and_merge_sync(
    messages,
    proxy_user_id,
    tenant_id,
    job_id,
)
```

The retriever should expose a method that returns domain prompt context:

```python
build_system_prompt_addition(...)
```

### Step 4: Register the Domain Overlay

Create an adapter in:

```text
api/services/domain_schemas/<domain>.py
```

It should implement:

```python
class AgriTechDomainSchema(BaseDomainSchema):
    def get_domain(self) -> str:
        return "agritech"

    def extract_overlay_sync(...):
        ...

    async def build_retrieve_context(...):
        ...
```

Then register it in:

```text
api/services/domain_schemas/registry.py
```

```python
DOMAIN_SCHEMAS = {
    "edtech": EdTechDomainSchema(),
    "agritech": AgriTechDomainSchema(),
}
```

After this, the extraction task and retrieve endpoint can discover the schema without domain-specific `if domain == ...` branches.

### Step 5: Add Universal Projections

If a domain contains facts that should help other approved agents, add a projection builder that returns `DomainMemoryProjection` objects:

```text
api/services/<domain>/projections.py
```

Projection builders should export only safe, portable summaries. For example, EdTech projects learning style, grade level, exam goal, and top strong or weak topics. It does not project detailed misconception history, full mock scores, or internal forgetting-curve state.

The domain adapter should call:

```python
DomainProjectionService().project_to_universal_sync(...)
```

Do not create a domain-specific universal memory table. All domains share `universal_memories`, with metadata such as `source_domain`, `source_field`, and `projection_key`.

Projection is automatically skipped unless the user has a UUI link and the current global agent has an active `read_write` grant.

### Step 6: Add API Views if Needed

If the dashboard needs a full profile screen, add a profile endpoint similar to:

```http
GET /v1/memories/edtech-profile?external_user_id=...
```

Domain profile endpoints are optional. The SDK-compatible `add()` and `get()` flow should work even without one.

### Step 7: Write Tests

Add tests for:

- Conflict router classification.
- Prompt builder compression.
- Extract-and-merge logic.
- Retrieval prompt formatting.
- Registry discovery.

Useful references:

```text
tests/unit/test_conflict_routing.py
tests/unit/test_edtech_extractor.py
tests/unit/test_forgetting_curve.py
tests/unit/test_domain_schema_registry.py
```

### Step 8: Submit a PR

Use this PR title:

```text
feat: add <domain> domain schema
```

Include:

- Five example conversations.
- Expected structured output.
- Passing tests.
- A short explanation of why each field belongs in structured domain memory instead of generic memory.
- A short explanation of which fields are projected into universal memory, if any.

## Domain Schema Design Principles

1. Keep generic memory active. Domain schemas are overlays, not replacements.
2. Every structured field must have a clear product reason to exist.
3. Do not duplicate what generic memory already handles well.
4. Personal facts should route to user-session clarification.
5. Shared organizational facts should route to tenant review.
6. Retrieval context should pass the domain-expert test: "Would I give this to a tutor, clinician, recruiter, or support agent before they respond?"
7. Domain extraction failure must not fail the whole memory job. The generic memory path should still complete.

## Minimal Contributor Checklist

Before opening a PR, confirm:

- Your conflict router is registered in `api/services/conflict_routing/registry.py`.
- Your domain overlay is registered in `api/services/domain_schemas/registry.py`.
- Generic extraction and retrieval still work when your domain overlay fails.
- Universal projections are consent-aware and use `DomainProjectionService`.
- Your migration can upgrade and downgrade cleanly.
- Your tests do not require real LLM calls.
