# Preference abstention diagnostic v1

This is an offline/development-only causal probe, not a release benchmark, a
calibration set or a new ingestion path. It responds to the interrupted
exploratory run's 319 saved replies: every reply abstained, including clear
adoptions, replacements and scoped exceptions. Passing the frozen validator
demonstrated format/policy acceptance, not useful interpretation.

## Fixed comparisons

Three public synthetic development cases are fixed in code before execution:

- `d0001`: explicitly adopt Python as the default for programming examples.
- `d0121`: explicitly replace a C++ default with Python.
- `d0151`: adopt Python for Atlas only, preserving the general C++ setting.

Each gets exactly three conditions, in the listed order:

| Condition | Response format | Synthetic user source kind |
| --- | --- | --- |
| `strict_client` | Strict JSON Schema | `client_assertion` |
| `json_client` | JSON object mode | `client_assertion` |
| `strict_direct` | Strict JSON Schema | `direct_user_input` |

The pinned model stays `gpt-4.1-mini-2025-04-14`, temperature 0, standard service
tier and at most 1,024 completion tokens. Instructions retain the original
wording and append the same explicit schema text to **all three** conditions.
This shared addition is a disclosed change from the historical experiment:
the control is a diagnostic control, not an exact replay of the original request.
Without the shared schema text, the JSON-mode arm would lose the supplied output
contract, introducing a second difference. The
[official Structured Outputs guide](https://developers.openai.com/api/docs/guides/structured-outputs)
describes JSON mode as JSON validity without schema guarantees. Both arms still
use the exact same frozen backend parser and validator.

Between `strict_client` and `json_client`, only `response_format` changes.
Between `strict_client` and `strict_direct`, only eligible synthetic user
`source_kind` values change. Source wording, turn roles, principal, scopes,
property catalogue, target identities, versions and authority remain identical.
This cannot justify relabeling real client assertions as direct user evidence.
No production attestation or permission rule changes.

## Execution and reporting

`benchmarks/internal/preference_abstention_diagnostic.py` has offline preparation
as its default. Run from the backend root:

```powershell
.venv\Scripts\python.exe -X utf8 -m benchmarks.internal.preference_abstention_diagnostic
```

Preparation rechecks the sealed public inputs, instructions, schema adapter and
frozen contract via the original base preflight. The new execution digest binds
all nine request hashes, their complete offline validation contexts, source-code
hashes, condition ordering and the $0.50 spending policy. No input, label, model,
case-selection or arbitrary-cap CLI option exists. Preparing a digest grants no
permission; an authorized operator must supply the approved diagnostic digest:

```powershell
.venv\Scripts\python.exe -X utf8 -m benchmarks.internal.preference_abstention_diagnostic --execute --approved-execution <diagnostic-execution-sha256>
```

The original journal is checked against its observed interruption hash. A derived
interrupted report corrects the counters without overwriting any original file.
The original `d0320` charge remains unknown; its $0.43 reservation is not a known
invoice charge, and its request is never retried by this diagnostic.

The existing shared executor enforces the smaller $0.50 cap. Before each request,
it durably reserves $0.43 using the published maximum context/output sizes and
standard uncached token rates. Charges are reconciled only from bounded, verified
usage and standard service tier. Any transport failure, invalid output, refusal,
truncation, validator rejection or uncertain billing stops the entire diagnostic.
No retries, fallback models, automatic reruns, extra smoke requests or post-result
instruction edits are permitted. The first control is also the schema smoke.

Artifacts stay ignored under `artifacts/preference-abstention-diagnostic/`.
The atomic `run-<diagnostic-execution-sha256>` directory is a permanent single-use
lock, even after a crash. Do not delete it to rerun. The original experiment has
a separate consumed lock; correcting code does not renew its authorization.
The journal is sealed before inspecting results.

Report validation passes separately from assessment-bearing responses, assessment
findings and abstention findings. An all-abstention run is not a useful extraction
success even when every response validates. A non-abstaining response still needs
source/meaning inspection: emitting an assessment alone is not semantic accuracy.
Counter tests and fake responses only prove control flow.

## Interpretation limits

This small English-only diagnostic is not independent human review. Each condition
has one observation per source case, so variation can motivate a hypothesis but
cannot establish a general causal effect or calibrated accuracy. The common schema
prompt addition can itself alter behavior versus the original run. If the control
now emits assessments, do not credit a different arm for solving a problem it
also shares with the control.

The diagnostic tests neither multilingual generalization nor adversarial safety,
retrieval, fresh SQL concurrency, production chat integration, or an end-to-end
latency SLA. AI draft annotations and private holdouts are never used. No memory
writes or production/SDK/MCP/assistant changes occur. All release gates stay unmet.
