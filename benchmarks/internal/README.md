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

## Phase 3A confirmation holdout

Phase 3A uses a separate single-use runner because every case must contain proposal content that was
not used by the visible development fixtures. The old generated v1 holdout is consumed and is not a
release gate.

A holdout custodian must prepare a private JSON file with:

- `schema_version: "2.0"` and `split: "holdout"`;
- frozen minimum counts per language and reference type;
- one group per evaluated reference type, including its expected outcome;
- object-shaped cases containing `language`, `utterance`, `target_ordinal`, and `proposals`;
- one to five proposals per case, each with non-empty `content`, expected normalized `memory`, and
  a production-supported `category`.

Accepted cases require a target ordinal within the supplied proposal list. The custodian must create
and label both the proposal content and confirmation wording independently of the development data,
then provide the dataset SHA-256 to the release operator without exposing case contents.

After development evaluation is approved, run the sealed pack exactly once:

```powershell
$env:MEMORYOS_HOLDOUT_APPROVAL = "approved-manual-holdout-run"
python -m benchmarks.internal.phase3a_holdout_release --allow-holdout --dataset <private-holdout-v2.json> --expected-sha256 <custodian-provided-sha256> --output artifacts/internal-benchmarks/phase3a/<new-result.json>
```

The runner verifies the checksum and complete dataset contract before claiming the pack, records a
single-use marker, rechecks the checksum during the claim and after evaluation, refuses to overwrite
a result, and records the checksum in both the result and marker. Schema or checksum failures do not
consume the pack. Once provider evaluation starts, a failed or interrupted run does consume it. Keep
the feature flag off unless the aggregate release gate passes; never inspect failed cases for tuning.
