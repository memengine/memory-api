# Preference exploratory preflight v1

This separate development-only experiment does not amend or satisfy the frozen
`preference-semantic-admission-v1` protocol. The archived annotation passes are
AI-generated drafts, not two independent human reviews or ground truth.

The implementation is `benchmarks/internal/preference_exploratory.py`. Its fixed
protocol and instructions are under `benchmarks/internal/protocols/`, named
`preference-exploratory-v1.json` and
`preference-exploratory-v1.instructions.txt`. That preflight module remains
read-only: it does not request credentials or make model calls. The separate
one-shot executor is described below; preparing it does not authorize execution.

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
or repeated runs is authorized by a single-run approval. The read-only preflight
does not itself implement an executor or a spending ledger.

## Separate one-shot executor

`benchmarks/internal/preference_exploratory_run.py` implements the development
executor. Its default command is offline and prints an execution plan:

```powershell
.venv\Scripts\python.exe -X utf8 -m benchmarks.internal.preference_exploratory_run
```

It requires the exact base preflight digest
`83345867ef8452189b04d3bf9c979dbd51382bbe82aff0a82ffae7e5817f2150`.
All 339 requests are rebuilt and checked against that plan before dispatch.
The only request-body amendment is `service_tier: default`. The official
[Chat Completions reference](https://developers.openai.com/api/reference/resources/chat/subresources/completions/methods/create)
states that an omitted tier inherits project configuration, whereas `default`
uses standard pricing. Thus the original request digests are **not** the paid
request digests. The executor binds amended bodies, its own source hash and
budget policy in a new `execution_sha256`. Explicit approval of that new digest
is required before execution; the old digest cannot authorize the CLI.

After that approval, use the exact digest in this command:

```powershell
.venv\Scripts\python.exe -X utf8 -m benchmarks.internal.preference_exploratory_run --execute --approved-execution <approved-execution-sha256>
```

The only credential source is the backend's existing server-side settings
(`OPENAI_API_KEY`, including the project's `.env`). Preparation does not load
credentials. Execution uses the fixed HTTPS OpenAI endpoint, direct HTTP with
zero retries, no redirects/proxy overrides, one call at a time, and a 30-second
HTTP timeout. No model fallback, subset selection, input path, label path or
resume option exists. HTTP timeouts bound network phases, not a production
end-to-end latency SLA.

The fixed $2 cap covers this run's token charges at the published standard text
rates, not unrelated account activity or an invoice guarantee. Before each call,
reserve $0.43 using the published 1,047,576-token context ceiling plus 1,024
completion tokens, rounded up to cents. This is deliberately much larger than
the expected small request cost. If known charges plus that reservation would
exceed $2, stop without another call. Successful usage must report standard tier,
nonnegative integer token counts within the reserved bounds and consistent totals.
The reconciled cost is an **uncached upper bound**; cache discounts are ignored.
Unknown/missing usage or tier retains the reservation and stops. Reconciled
charges for a refusal or invalid output are retained, not erased.

Before dispatch, an exclusive run directory is created at
`artifacts/preference-exploratory/run-<execution-sha256>/`, and a reservation is
written, flushed and synced to `journal.jsonl`. That directory is a permanent
single-use lock, including after interruption. Do not remove it to resume or
retry. The first paid case doubles as schema smoke and is never repeated.
The same frozen validator checks every reply using the fixed offline fixture
as both shown and current snapshot. Any rejection stops the run and preserves
its reason; a policy rejection is not necessarily a semantic-model error.
This is not a live SQL ownership/concurrency test.

The manifest, journal and summary are ignored local artifacts. The journal stores
synthetic responses and decisions, never credentials or HTTP headers. Completion
or stopping seals the journal hash in the summary. A crash may leave only the
manifest and pending reservation; it still consumes the authorization and must
not cause an automatic retry. AI drafts and holdouts are never loaded by this
executor. Tests with mocked responses demonstrate control flow and spending
safeguards, not schema acceptance by OpenAI or semantic accuracy.

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
