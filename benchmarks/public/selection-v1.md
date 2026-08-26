# Public benchmark selection v1

Date: 2026-08-26

## Decision

Start with **LongMemEval cleaned, small track**. Follow with **LoCoMo QA** as an
independent conversational-memory check. Do not begin with the largest or newest benchmark.

## Why LongMemEval first

The official ICLR 2025 benchmark contains 500 questions across information extraction,
multi-session reasoning, knowledge updates, temporal reasoning, and abstention. Its online
history-ingestion followed by question answering maps directly to MemoryOS add/retrieve behavior.
The cleaned release and official evaluator provide a stable first external measurement.

Upstream: https://github.com/xiaowu0162/LongMemEval

Initial scope:

- cleaned small dataset only;
- official question categories and official scoring retained;
- real MemoryOS production API path for ingestion and retrieval;
- fixed answer model and judge model recorded separately from MemoryOS;
- per-question ingestion, retrieval, answer, latency, token, and cost artifacts;
- no prompt or product tuning during the baseline.

## Second benchmark

Run LoCoMo QA after LongMemEval. LoCoMo supplies ten long conversations with timestamped
sessions, annotated questions, answers, categories, and evidence dialog IDs. It adds a useful
evidence-grounding view and an independently designed conversational corpus.

Upstream: https://github.com/snap-research/locomo

## Later benchmarks

- **MemoryAgentBench**: broader agent-memory abilities; add after the conversational adapters are stable.
- **LongMemEval-V2**: valuable experiential/workflow benchmark, but its trajectories reach very large
  haystacks and its accuracy-latency frontier makes it a later capacity-aware phase.
- **MemoryBench (ICML 2026)**: useful for feedback-driven continual learning, but broader and more
  expensive than the initial launch comparison.

Upstreams:

- https://github.com/HUST-AI-HYZ/MemoryAgentBench
- https://github.com/xiaowu0162/LongMemEval-V2
- https://github.com/THUIR/MemoryBench

## Required first adapter contract

The LongMemEval adapter must expose three isolated operations:

1. ingest timestamped sessions through MemoryOS;
2. retrieve bounded evidence for each benchmark question;
3. produce the official answer-file schema without modifying upstream evaluation code.

Every run must record:

- MemoryOS commit and migration head;
- upstream repository and dataset revisions;
- extraction, embedding, answer, and judge providers/models;
- prompts and retrieval parameters;
- per-stage latency, calls, tokens, and estimated cost;
- official overall and category scores;
- failures split into product, provider, adapter, and evaluator categories.

## Guardrails

Public datasets and results must be ignored by Git. The adapter must refuse to run when an internal
holdout path is supplied. A smoke subset may validate wiring, but it must not be used for tuning or
reported as the benchmark result. The first scored baseline is immutable once captured.
