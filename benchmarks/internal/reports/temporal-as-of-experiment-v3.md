# Optional `as_of` retrieval experiment v3

## Change

Added an optional timezone-aware `as_of` field to tenant memory retrieval. Requests without
the field retain the existing cache, hot-tier, Qdrant, PostgreSQL fallback, ranking, and
response behavior.

When `as_of` is supplied, PostgreSQL is authoritative. A memory is eligible when:

- `effective_from` is null or at/before `as_of`;
- `effective_until` is null or after `as_of`; and
- it is active, or an archived revision has at least one explicit validity boundary.

This permits explicitly bounded superseded revisions to be read historically without making
them visible in normal retrieval. Tenant/user, agent, category, limit, and optional ingestion
age filters remain enforced. Historical requests have date-specific cache identities and do
not consume current cached results.

## Verification

- As-of, temporal diagnostics, retriever, and vector-store tests: 30 passed.
- Mocked API route/request regressions: 5 passed.
- Frozen temporal benchmark: 18 scenarios; 17 product-evaluable; 17 passed; zero product
  failures; one environment harness error; product success 100%.
- Holdout was not accessed.

The remaining harness error is the host subprocess lacking `DATABASE_URL` for the PostgreSQL
source-event deduplication scenario. It is unrelated to `as_of` behavior.

## Known limitation

Superseded vectors are intentionally deleted from Qdrant. Historical results are therefore
selected by PostgreSQL validity and ordered by importance, effective start, and access time;
they do not currently receive semantic vector ranking. This avoids changing Qdrant lifecycle
or superseded-memory behavior in this isolated experiment.
