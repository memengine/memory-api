# Retrieval Tuning

## Current Hybrid Weights

- semantic: 0.60
- importance: 0.25
- recency: 0.15

## Tuning Notes

- Kept the original 0.60 / 0.25 / 0.15 balance because manual spot-checks already return the Python preference memory in the top 3 for the query `programming language preferences`.
- The benchmark harness measures cache-miss latency with local deterministic embeddings so the results reflect retrieval-system performance rather than remote embedding API latency.
- Background access updates remain asynchronous via Celery dispatch; the benchmark compares this non-blocking path against a simulated blocking inline update to confirm the response path stays faster.

## Latest Benchmark Snapshot

- 1,000 memories: p50=8.16 ms, p99=9.34 ms
- 10,000 memories: p50=8.23 ms, p99=15.09 ms
- 100,000 memories: p50=14.62 ms, p99=17.28 ms
- cache hit median latency: 0.01 ms
- manual relevance top 3: ['User prefers Python for backend work', 'User prefers Go for systems programming', 'User uses PostgreSQL for analytics']
- cold start returned: ['User is an engineer', 'User works in healthcare']
- non-blocking vs blocking update: 8.56 ms vs 58.52 ms
- the benchmark script measures retrieval-system performance with deterministic local embeddings, so the latency numbers exclude live Gemini network time and reflect the retriever itself.