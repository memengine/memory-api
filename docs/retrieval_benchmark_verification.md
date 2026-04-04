# Retrieval Benchmark Verification

## Weights

- semantic weight: 0.60
- importance weight: 0.25
- recency weight: 0.15

## Cache Miss Benchmarks

- 1,000 memories: p50=8.16 ms, p99=9.34 ms
- 10,000 memories: p50=8.23 ms, p99=15.09 ms
- 100,000 memories: p50=14.62 ms, p99=17.28 ms

## Verification Checklist

- p50 retrieval under 20ms at 10K memories: True
- p99 retrieval under 50ms at 100K memories: True
- cache hit path under 5ms: True
- manual relevance query returns Python in top 3: True
- cold start user with 2 memories returns both: True
- background access update does not block response: True

## Details

- cache hit median latency: 0.01 ms
- manual relevance top 3: ['User prefers Python for backend work', 'User prefers Go for systems programming', 'User uses PostgreSQL for analytics']
- cold start returned: ['User is an engineer', 'User works in healthcare']
- non-blocking background update median latency: 8.56 ms
- blocking inline update median latency: 58.52 ms
