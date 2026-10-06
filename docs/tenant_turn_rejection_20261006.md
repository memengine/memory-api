# Single fresh rejection: recall is not reaffirmation

Date: 2026-10-06. Baseline: `fd4ca63`.
Exactly one authorized fresh synthetic diagnostic; no batch rerun, provider
fallback/retry, prompt/schema modification, activation, SDK/assistant change,
deployment or live flag change.

## Confirmed cause for this case

The synthetic user asked:

> Which programming language is already stored as my usual choice for coding examples?

A backend-bound snapshot contained a global C++ default. The unchanged runtime
prompt distinguishes explicit reaffirmation from durable preference setting and
instructs value-free actions to use `null`.

OpenAI returned this valid JSON:

```json
{
  "preferences": [{
    "action": "reaffirm",
    "property_key": "examples.language",
    "scope_ref": "global",
    "value": "C++",
    "turn_index": 0,
    "quote": "Which programming language is already stored as my usual choice for coding examples?"
  }],
  "nonpreferences": []
}
```

This response has two distinct defects:

1. Asking what is stored is not an explicit instruction to keep/reaffirm it.
   The model proposed a preference operation for a recall question.
2. `C++` comes from stored context, not the quoted user question. The model also
   supplied a value for the value-free `reaffirm` action.

The observed failure is therefore not malformed JSON, an unavailable provider,
an absent memory or a failure to match a particular spelling. It is a model
proposal that breaches the meaning/evidence contract. This one observation does
not explain every rejection in the earlier 36-case run or establish an overall
model accuracy rate.

## Exact gate and error

`PreferenceIntentReply.model_validate_json()` invoked
`PreferenceIntent.represented_intent()` in
`api/services/preference_intent_contract.py`:

```text
preferences.0
Value error, intent requires verbatim evidence
```

The immediate condition is `value not in quote`. Parsing stopped there, before
the separate check that value-free actions cannot supply a value. The runtime
classifier wrapped that original validation exception as
`PreferenceAdmissionUnavailable("intent_response_invalid")`; the complete
cause chain and untouched provider response are retained locally.

The JSON Schema supplied to the provider describes field shapes and action
enums. It does not encode all Python cross-field/source validators. A structured
response is not independently verified meaning or evidence.

## Offline controls: no further provider calls or writes

Using the captured response and a separate in-memory copy:

- The untouched response reproduced the same verbatim-evidence validation error.
- Setting only `value` to `null` in the diagnostic copy passed schema validation,
  but production evidence binding then rejected it with
  `intent evidence rejected: unsupported_user_evidence`.
- An empty reply (`preferences: []`, `nonpreferences: []`) passed binding and
  produced no declarations, choice operations or nonpreference findings.

The null-value copy was never substituted for the real response, applied to a
memory, or counted as a repaired pass. The binding controls used a synthetic
valid offered identity and the captured actual user message, not an activation
transaction or a second live sample.

This proves why clearing the value or weakening the evidence validator is not
a correct repair. The recall question still supplies no reaffirmation evidence.
The backend must continue rejecting unsupported operations.

## Release consequence

The confirmed target is the proposal/disposition boundary: the model must
distinguish requesting existing context from asserting, changing or reaffirming
that context. MemoryOS must not let stored values become user evidence.

Do not add a phrase-specific recall regex, silently coerce an invalid reply to
"no change", release an unknown guard, or auto-repair the model's output. No
backend validator weakening is justified by this diagnosis. A stronger model or
an improved task/schema is a candidate to measure, not an established fix.

The separate latency result remains: earlier dispatch plus a 750 ms worker wait
does not pass the tested configuration. This diagnosis does not authorize a
new architectural phase, a changed release threshold or production activation.
The fixed pilot criteria and existing production behavior remain unchanged.

## Evidence, cost and isolation

- Model alias: `gpt-4o-mini`; provider returned snapshot
  `gpt-4o-mini-2024-07-18`.
- One provider dispatch, one response; 1,353 input and 52 output tokens, with
  zero reported cached prompt tokens.
- Observed transport wall time: 2,863.46 ms. One sample is not p95/p99 or
  end-to-end first-token latency.
- Estimated token cost: **$0.00023415**, under the $0.01 single-case ceiling;
  no unknown-charge reservation remains. Combined with the preceding
  feasibility run: **$0.009351**. This is token-based accounting, not an invoice.
  Rates were checked against
  [official OpenAI documentation](https://developers.openai.com/api/docs/models/gpt-4o-mini).
- Real local HTTP admission, source/capture ownership and detached worker input;
  a dedicated disposable PostgreSQL/Redis environment and fresh synthetic
  tenant/user. No customer records, live circuit state or signed-in browser data.
- Classified once after capture. `admit()`/`apply()` were never called and no
  model-proposed preference was activated. The initial admission retained its
  processing guard until disposable test data was removed.

Ignored artifacts under `artifacts/turn-rejection-single-20261006/` preserve
`diagnostic.py`, `result.json`, `started.once`, and provider-free
`offline_checks.py`. Request data contains only synthetic content; credentials
and HTTP authorization headers were not persisted.

SHA-256:

- Diagnostic: `82bfff34cf6967c9cfb29ba7d5fb2e275737d5640812a48a5d5bcc95f1b4f8b2`
- Request body: `6025813cf2f48dfabfef40639e0e85cf042ac63c5e956c0b0b6e40e5395b081b`
- Result: `7a09a074624245f873a4ac2109868e9cc9850d17a5965ab61ddc5ac19010631e`

The preceding failed batch and its hashes were not changed. Disposable test
containers/data were removed; diagnostic artifacts remain for review.
