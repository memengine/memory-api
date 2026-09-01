# MemoryOS internal extraction benchmark

This framework is for private correctness and regression testing only. It must not be used for public or marketing benchmark claims.

- `datasets/extraction/development`: visible cases used while developing the evaluator.
- `datasets/extraction/holdout`: locked cases; never copy them into production prompts or extraction specifications.
- `schema`: versioned case and result contracts.
- `baselines`: reviewed regression floors. Deterministic expected-output baselines are contract checks, not model-quality claims.

The 16 cases in `tests/evals/general_extraction_cases` remain supported through `load_legacy_cases`. Generated run records should go to `artifacts/internal-benchmarks/<run-id>/` and should not be committed as source data.

Post-extraction evidence attribution is isolated in `api/services/evidence_attribution_service.py`. It receives immutable memory content plus original conversation turns and returns only memory-index to turn-index mappings. The reviewed three-run development baseline is `baselines/post-extraction-evidence-development.json`.

The offline deterministic importance experiment is isolated in `deterministic_importance.py` and `offline_importance.py`. It uses rubric-derived semantic features, makes no provider calls, and is not wired into production. Its accepted development-only baseline is `baselines/deterministic-importance-development.json`.

The independent 22-case importance generalization pack is `datasets/extraction/development/generalization_v1.jsonl`. It was frozen before evaluating the deterministic scorer. Results are recorded in `baselines/deterministic-importance-generalization.json`; the scorer did not pass the generalization guards and is therefore not approved for production integration.

## Consolidated regression tiers

`benchmark-manifest-v1.json` is the versioned registry for executable suites, frozen thresholds,
accepted baselines, infrastructure requirements, and component activation status.

```powershell
python -m benchmarks.internal.orchestrator --tier fast
python -m benchmarks.internal.orchestrator --tier integration
python -m benchmarks.internal.orchestrator --tier provider --approve-provider
```

The provider tier is never part of ordinary PR CI. Holdout is excluded from all three tiers and
requires a separately reviewed manual command plus dual authorization. Aggregate JSON and Markdown
reports are written under `artifacts/internal-benchmarks/aggregate/<run-id>/`.
