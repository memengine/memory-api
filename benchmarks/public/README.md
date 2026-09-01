# MemoryOS public benchmark track

This directory is intentionally separate from `benchmarks/internal`.

Rules:

- Never copy internal development or holdout cases into public benchmark adapters.
- Never load the internal holdout from a public benchmark command.
- Pin the upstream dataset revision, evaluator revision, provider/model, prompts, and configuration.
- Store downloaded datasets and generated results outside Git.
- Preserve raw outputs and report failures; do not tune against public test labels.
- Public benchmark results are technical evaluation evidence, not automatically approved marketing claims.

The first implementation target was the cleaned LongMemEval small track. LoCoMo
was then used as an independent one-question diagnostic. Both confirmed that the
current product does not provide transcript-complete episodic recall, which is
deferred to a later product version.

See `selection-v2.md` for the current scope decision. Do not expand LongMemEval or
LoCoMo, and do not force an adapter onto a benchmark whose information model does
not match the production API.

## LongMemEval adapter

Validate a downloaded official cleaned dataset without provider calls:

```bash
python -m benchmarks.public.longmemeval.runner \
  --dataset benchmarks/public/data/longmemeval_s_cleaned.json \
  --mode validate
```

Live smoke and full modes use the real MemoryOS add/job/retrieve path and therefore require
`MEMORYOS_BENCHMARK_API_URL`, `MEMORYOS_BENCHMARK_API_KEY` (or the existing
`BENCHMARK_API_KEY` alias), and the explicit
`--approve-provider-calls` flag. The smoke subset is selected only from a hash of `question_id`;
gold answers and evidence labels are never sent to MemoryOS.

Add `--answer-eval --approve-answer-eval-calls` to generate answers with the pinned
`gpt-4o-2024-08-06` snapshot and run a task-aware preview judge. This paid stage records
latency, tokens, estimated cost, per-type preview accuracy, and writes an adjacent
`.hypotheses.jsonl` file. Preview accuracy is not reported as an official LongMemEval
score: independently grade the hypotheses with the upstream `evaluate_qa.py` script.

## Current status

- LongMemEval: closed as an episodic architecture diagnostic.
- LoCoMo: closed after one frozen smoke case; no official score claimed.
- MemoryAgentBench FactConsolidation: investigated but not selected. Its official
  data is arbitrary world-knowledge consolidation, while the current MemoryOS
  extraction contract is user/customer-state focused. Entity-to-fake-user mapping
  or benchmark-fact rewriting would not be an honest official evaluation.
- Next external adapter: none selected until contract fit is demonstrated from the
  official dataset and protocol.
