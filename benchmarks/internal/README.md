# MemoryOS internal extraction benchmark

This framework is for private correctness and regression testing only. It must not be used for public or marketing benchmark claims.

- `datasets/extraction/development`: visible cases used while developing the evaluator.
- `datasets/extraction/holdout`: locked cases; never copy them into production prompts or extraction specifications. Only the directory marker is tracked; the real `.jsonl` pack is ignored, provisioned only for an approved manual run, and must never be committed.
- `schema`: versioned case and result contracts.
- `baselines`: reviewed regression floors. Deterministic expected-output baselines are contract checks, not model-quality claims.

The 16 cases in `tests/evals/general_extraction_cases` remain supported through `load_legacy_cases`. Generated run records should go to `artifacts/internal-benchmarks/<run-id>/` and should not be committed as source data.

Live extraction evaluation reads the citations already validated by `api.services.extraction_service.ExtractionService`; it never makes a second attribution-model call. This keeps evidence accuracy, latency, and cost measurements aligned with the production extraction path.

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

## Sealed holdout release protocol

The holdout pack is not a CI fixture and must never be restored from Git. The command below is a
manual release operation, not a routine benchmark tier:

```powershell
$env:MEMORYOS_HOLDOUT_APPROVAL = "approved-manual-holdout-run"
python -m benchmarks.internal.holdout_release --allow-holdout
```

Before this command, all of the following are required:

1. A holdout custodian provisions the approved pack at the ignored path and records its checksum in
   the private release record. The custodian does not change prompts, specifications, or code after
   seeing the cases.
2. A separate release approver confirms the target commit has green unit tests, FAST benchmarks, and
   reviewed development-provider results. The flag and environment value are an accidental-run guard;
   the two-person approval is recorded outside the repository.
3. Run in a clean checkout with provider credentials supplied through the operator environment. Do
   not print cases, prompts, predictions, or the resulting artifact to CI logs.
4. Store the full artifact in restricted release storage, review only aggregate metrics, then remove
   the local sealed pack. Never commit the pack or its detailed result artifact.
5. Any material regression opens a release-blocking issue. Do not tune prompts against holdout cases;
   return to the visible development set and repeat the approval process for the next blind run.

`benchmarks.internal.holdout_release` uses the same production-path evaluator as the development
runner, but only the manual loader can open the sealed dataset. This makes the release procedure
reproducible without making the data available to ordinary development or CI.
