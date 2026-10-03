# Preference exploratory preflight v1

This separate development-only experiment does not amend or satisfy the frozen
`preference-semantic-admission-v1` protocol. The archived annotation passes are
AI-generated drafts, not two independent human reviews or ground truth.

The implementation is `benchmarks/internal/preference_exploratory.py`. Its fixed
protocol and instructions are under `benchmarks/internal/protocols/`, named
`preference-exploratory-v1.json` and
`preference-exploratory-v1.instructions.txt`. No provider client or execute
command exists. It does not request or transmit credentials or make model calls.

## What the dry run does

```powershell
.venv\Scripts\python.exe -X utf8 -m benchmarks.internal.preference_exploratory --dry-run
```

Run from the backend repository root. The command only prints JSON; it does not
write files. If exporting on Windows, use explicit UTF-8 decoding and encoding,
not a terminal's legacy code page. The report contains hashes, not raw requests.

It verifies the fixed public development input seal and all three frozen contract
assets before preparing requests. All 360 cases remain in the manifest. The
existing `model_input` filter yields 339 candidate calls and 21 no-call controls.
Nothing reads the AI drafts, human worksheets or private holdouts. There is no
caller-supplied input/label path or dynamically selected subset.

Request bodies contain only fixed instructions, the frozen source projection,
and a case-bound response schema. Case IDs are recorded locally, never sent in
the source JSON. Author tags, annotations, owner IDs, memory IDs, authority,
versions and expected answers are excluded. Embedded forged source instructions
remain source text, not system instructions.

The report binds input, protocol, instructions, preflight code, frozen assets,
and every request/schema digest. `preflight_sha256` hashes the complete report
excluding only that field. A changed model, input, instruction, request or adapter
needs a new preflight and explicit execution approval; a digest is not permission.

## Candidate and schema adaptation

The proposed candidate is OpenAI `gpt-4.1-mini-2025-04-14`, using Chat Completions
with temperature 0 and at most 1,024 completion tokens. This is a pinned,
non-reasoning baseline, not a claim that it is the best model or that our account
can call it. The official [model documentation](https://developers.openai.com/api/docs/models/gpt-4.1-mini)
lists its snapshot, Structured Outputs support and standard rates of $0.40 input
and $1.60 output per million text tokens (verified October 4, 2026).

The frozen contract emits standard JSON Schema, not a provider-certified dialect.
The [Structured Outputs documentation](https://developers.openai.com/api/docs/guides/structured-outputs)
describes the supported subset, including nested anyOf, required fields and
closed objects. Our narrow adapter converts only the kind-tagged assessment/
abstention union from oneOf to anyOf, drops its discriminator annotation and
replaces the two kind constants with singleton enums. Branches are mutually
exclusive by kind; unexpected branch changes are rejected. All evidence bounds,
offered references and other constraints remain unchanged.

Unit tests establish that exact transformation, not live API acceptance or
semantic correctness. `request_schema_provider_verified` stays false until a
separately approved live smoke test. Every future model reply still needs the
unchanged frozen parser, source attribution and snapshot/policy validation.

## Cost and future execution boundary

The dry run computes a deliberately pessimistic planning proxy: each entire
UTF-8 request body's byte count plus 1,024 units of framing padding. It prices
that proxy at uncached input rates, plus every request's maximum output tokens.
This is not exact token counting, a guaranteed billed-token upper bound or a
hard spending cap. Provider serialization is not established by a local byte
count. No tokenizer assets are downloaded, no cached-input savings assumed and
no existing provider budget authorization reused.

`planning_cost_usd` is an estimate for approval planning;
`approved_spending_cap_usd` remains null. The report always records zero actual
calls/cost and false execution, human-release, accuracy and production flags.

Before a live run, separately approve the exact preflight/model, an explicit USD
cap, synthetic-source sharing, token accounting/reservation strategy and output
location. A future executor must enforce one attempt per eligible case, no SDK
retry, no model fallback, one in-flight call, a 30-second timeout, and stop on
transport failure, refusal, truncation or invalid output. Uncertain billing must
not trigger a blind retry. Freeze results before reviewing them; no prompt tuning
or repeated runs is authorized by a single-run approval. This preflight does not
implement that executor or a ledger enforcing the spending cap.

## What an eventual report may claim

Report schema/attribution failures, policy rejections, provider refusals,
truncation, usage, actual cost and descriptive run latency. Latency on these
synthetic calls is not a production p99 SLA. Related language variants remain
correlated, not independent samples.

After predictions are sealed, disagreements with AI drafts may guide inspection.
Do not label disagreement as an error or agreement as accuracy. Do not calculate
ground-truth precision/recall, calibrated confidence or release readiness from
these drafts. Do not drop ambiguous/unsupported cases to improve a result.
All human-review, adjudication, safety, concurrency and release requirements
remain unmet by this exploratory experiment. Production, SDK, MCP and assistant
behaviour are unchanged; no deployment is needed.
