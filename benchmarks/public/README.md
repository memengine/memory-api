# MemoryOS public benchmark track

This directory is intentionally separate from `benchmarks/internal`.

Rules:

- Never copy internal development or holdout cases into public benchmark adapters.
- Never load the internal holdout from a public benchmark command.
- Pin the upstream dataset revision, evaluator revision, provider/model, prompts, and configuration.
- Store downloaded datasets and generated results outside Git.
- Preserve raw outputs and report failures; do not tune against public test labels.
- Public benchmark results are technical evaluation evidence, not automatically approved marketing claims.

The first implementation target is the cleaned LongMemEval small track. See
`selection-v1.md` for the selection rationale and execution order.
