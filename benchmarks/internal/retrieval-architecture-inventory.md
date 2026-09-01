# Retrieval correctness architecture inventory

Scope: core tenant-memory backend, development data only. Holdout, SDK, MCP, dashboard, public benchmarks and load testing are excluded.

## Real production flow

`POST /v1/memories/retrieve` resolves the tenant proxy user and invokes `RetrieverService.retrieve`. The service checks L1/Redis caches and quota mode, merges a PostgreSQL-backed hot tier, uses a cold-start PostgreSQL path below five active memories, embeds the query using the configured embedding model, searches tenant/proxy-scoped Qdrant collections, applies category/time filters, hydrates from PostgreSQL when vector payloads are incomplete, computes hybrid semantic/importance/recency scores, deduplicates, queues asynchronous access updates, caches results, returns provenance, builds prompt context, and logs a retrieval event.

## Existing infrastructure

- `tests/unit/test_retriever.py`: cache, cold-start, hybrid scoring, deduplication, category filters, quota modes and embedding failure.
- `tests/unit/test_retriever_service.py`: time filter and PostgreSQL fallback.
- security tests: tenant, user, category and agent isolation.
- `tests/performance/benchmark_retrieval.py` and `scripts/benchmark_retrieve_latency.py`: synthetic and deployed latency/load checks.
- `tests/integration/test_retrieval_quality.py`: currently only weight-sum and overfetch-cap assertions; it is not a relevance evaluation.

The new internal dataset extends these deliberately. It does not replace the latency harness. Its first layer freezes ranker/filter/lifecycle/provenance correctness using controlled vector similarity scores so ranking regressions are distinguishable from embedding-model or Qdrant failures. A subsequent live-vector layer should use the configured embedding provider and real Qdrant against the same development concepts.

## Metrics

Precision@K, Recall@K, MRR, nDCG@K, empty-result accuracy, superseded-memory leakage, filter leakage, duplicate-result rate and provenance preservation. Live-vector evaluation will additionally measure candidate recall, API success, cache state, latency, embedding tokens where reported, and cost.
