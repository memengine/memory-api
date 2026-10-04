# Preference schema-order diagnostic v1

This is a prepared development-only experiment, not a production repair. It
requires a new explicit approval before provider calls. Neither the interrupted
exploratory authorization nor the stopped nine-condition diagnostic can be reused.
Preparing a new digest does not grant authorization.

## Evidence and hypothesis

The previous diagnostic stopped after two of nine requests. Strict mode abstained
on the first adoption case. JSON mode identified Python but incorrectly bound it
to the unrelated `answer.detail` target. The frozen backend validator rejected
that output with `target_identity_mismatch`. This demonstrates a working safety
gate, not a successful interpretation or a reason to switch to JSON mode.

The shared serializer recursively sorts object keys. Consequently the strict
wire schema's `Assessment.properties` begins with `evidence`, despite the frozen
contract defining `kind` first. `Abstention.properties` already begins with `kind`.
The [official Structured Outputs guide](https://developers.openai.com/api/docs/guides/structured-outputs)
states that schema key ordering determines output key ordering. That makes this a
testable decoding-order hypothesis; it does **not** establish why the model
abstained. The JSON-mode assessment also began with `evidence`, and the prior
single pair does not establish a general causal effect.

## One changed factor

Reuse the same three fixed English development cases and the existing
`strict_client` prompt preparation: adoption (`d0001`), replacement (`d0121`),
and scoped exception (`d0151`). Each has two conditions in this fixed order:

1. `sorted_control`: exact existing canonical request bytes.
2. `kind_first`: move only `kind` to the first position in
   `response_format.json_schema.schema.$defs.Assessment.properties`.

All other bytes stay identical within each pair: instructions and explicit schema
text in the prompt, user wording, roles, source kind (`client_assertion`), catalogue,
scope, target, union branch order, required arrays, schema bounds, model snapshot,
temperature, standard service tier, and 1,024-token completion limit. The textual
schema in the prompt remains unchanged intentionally, so only the strict schema's
property order is tested. The treatment does not restore every original contract
field's order or modify the frozen contract.

## Exact-byte safeguards

Canonical request hashes are equal for an order-only pair and therefore cannot
verify this experiment alone. Each plan row also seals a SHA-256 of the exact
UTF-8 request body. The shared executor checks complete body coverage, immutable
bytes, the byte hash, and parsed equality with the approved request before creating
the one-shot lock. It snapshots the supplied body mapping, then sends those exact
bytes without re-sorting. Tests verify actual mock-transport request content and
prove that replacing only the changed properties block restores the control bytes.
Existing experiments retain their default sorted serialization.

## Preparation and execution

From the backend root, preparation is offline and creates no provider client:

```powershell
.venv\Scripts\python.exe -X utf8 -m benchmarks.internal.preference_schema_order_diagnostic
```

The plan binds six request bodies, full validation contexts, source code, fixed
input/prompt preparation and the $0.50 cap. No arbitrary dataset, model, case,
instruction, body or cap override is exposed. Only after explicit approval of
that exact digest may the operator use:

```powershell
.venv\Scripts\python.exe -X utf8 -m benchmarks.internal.preference_schema_order_diagnostic --execute --approved-execution <new-execution-sha256>
```

It reuses the existing durable $0.43 per-attempt reservation, bounded usage checks,
no-retry transport, frozen validator, separate finding/abstention counters and
stop-on-failure policy. Six is the maximum, not a guaranteed completed count: any
transport/usage/output/validator failure or unavailable budget reservation ends
the run, potentially before a pair completes. A pending charge retains its
reservation. Artifacts remain ignored under
`artifacts/preference-schema-order-diagnostic/`; the atomic run directory is a
permanent one-shot lock even after interruption. Existing journals are not read,
rewritten or resumed by preparation.

## Interpretation limits

The follow-up decision is deliberately bounded:

- If both conditions abstain, moving `kind` first was insufficient on the tested
  cases. Do not automatically try more key permutations.
- If both produce usable assessments, the previous abstention was not reproduced;
  do not attribute success specifically to the treatment.
- If only the treatment produces usable assessments, report a development signal
  of order sensitivity, not a production fix or calibrated accuracy.
- If either condition produces a rejected assessment, stop and inspect that exact
  failure. Do not weaken the validator or silently continue with another variant.

Any further paid experiment requires a new evidence-based plan and approval.

No human labels, AI draft annotations or private holdouts are loaded. One observation
per condition on three familiar English development cases cannot establish
multilingual accuracy, calibration, adversarial safety, integration readiness or
production latency. Abstentions passing validation do not count as successful
interpretation. Assessments need source/meaning inspection even if the mechanical
gate accepts them. A different response may suggest order sensitivity but is not
by itself proof of the root cause. Production, SDK, MCP, assistant behavior and
release gates remain unchanged. Mocked tests establish preparation and transport
safety only, not model quality.
