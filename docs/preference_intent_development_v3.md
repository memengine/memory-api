# V3 intent development preparation: no provider execution approval

Prepare with `.venv/Scripts/python.exe -m benchmarks.internal.preference_intent_development_v3 --dry-run`.
To create the new ignored local artifact, add `--save`. It exclusively creates
`artifacts/preference-intent-development-v3/prepared-v3.json`; it does not overwrite
a prior preparation. The CLI has no execute flag, reads no credential and creates
no client. Default preparation prints the plan without writing files.

## Fixed scope and separation

Reuse the existing v2 preparation for frozen-asset/candidate checks and the same
24 familiar public synthetic cases: eight related groups, each with English,
Hindi and Hinglish variants (8 per language). These are unreviewed development
inputs, not independent gold labels, human review or a blind holdout. The first
three cases remain `d0001`, `d0121`, `d0151`; their order is fixed before responses.
Never load private holdouts, annotations, worksheets or consumed run artifacts.

Rebuilding public fixture contexts does not reuse an old execution authorization.
The v3 requests use the six-field intent schema and one generic prompt defining
source meaning separately from database state. No case ID, family, author tags,
expected answer, target ID, authority or coverage assertion enters model input.
There are no per-case hints, keyword/regex recognizers, confidence gate, model
vote, response repairs or production memory writes.

The narrow strict-schema adapter changes only the discriminated finding union
(`oneOf` to nested `anyOf`, removing its discriminator) and fixed kind constants
to singleton enums. It preserves required nullable value and evidence bounds,
and emits `kind` first. [Official Structured Outputs guidance](https://developers.openai.com/api/docs/guides/structured-outputs)
documents required fields, closed objects, nested unions and schema-key ordering.
Preparation alone does not prove the pinned provider accepts these exact bytes.

## Seal and candidate

The new digest covers canonical requests, exact UTF-8 wire bytes/order, complete
fixture contexts including absence, prompt, candidate protocol, all selected
public inputs, manifest, contract/runner/preparation code and direct evidence
policy/schema dependencies. V1/v2 frozen hashes are verified without amending
them. V3 contract/docs/prompt and execution policy are source-sealed in the plan.
Any edit requires re-preparation and a new separately approved digest.

Keep `gpt-4.1-mini-2025-04-14`, Chat Completions, temperature 0, max output 1,024,
one result and explicit default service tier. No provider/model migration is part
of this preparation. [Official GPT-4.1 mini documentation](https://developers.openai.com/api/docs/models/gpt-4.1-mini)
lists this snapshot, structured output support, the 1,047,576-token context window
and standard input/output rates of $0.40/$1.60 per million tokens, checked October
4, 2026. A future invocation must recheck availability/pricing before approval.

Proposed future run: at most 24 attempts, sequential, $0.50 hard cap, zero retries
or fallback and the shared $0.43 conservative per-attempt reservation. Byte counts
are only a planning proxy, not billed-token proof or a spending guarantee. Reuse
the existing durable reservation/one-shot lock, exact-body dispatch, bounded
envelope, 30-second HTTP phase timeout and usage reconciliation. Transport,
refusal, truncation, invalid envelope/usage or validator rejection stops the run.
Unknown billing retains its reservation; interruption consumes the authorization.

## Do not count reviews as successful handling

The existing executor selects v3 only by its fixed validator/mode, source seals,
complete contexts, exact request/wire/context hashes, case bounds and stop policy.
Its default v1 CLI does not execute v3. There is no injectable validation callback
or second executor. V3 findings remain `proposals`, never coerced into v1/v2 stances.

Record intent proposal findings, every backend candidate-route count, abstentions
and rejections separately. A usable candidate count includes new, duplicate,
replacement, withhold and reaffirm candidates; it excludes review and no-current-
change routes. After the first three fixed adoption/change smoke cases, stop if
all have zero usable candidates. This is a predeclared structural futility check,
not semantic correctness, mutation permission or a release threshold. An accepted
but wrongly labelled intent remains a semantic failure to investigate, not a pass.

The raw reply, bound proposal and backend review reason remain in the durable
journal. Do not relabel missing-target explicit change as adoption, silently repair
old failures, mark undispatched cases successful, or tune during a run. Report
attempts versus unexecuted cases, exact observations, latency and cost honestly.
No automated accuracy/calibration or production p99 claim is available from these
unlabelled familiar cases; independent human-reviewed evaluation remains pending.

## Separate execution approval

Preparation authorizes no paid call or synthetic-source sharing. Future approval
must name the new digest, pinned model, maximum 24 attempts, $0.50 cap and output
directory `artifacts/preference-intent-development-v3/run-<digest>/`. Before use,
compare the saved plan with a fresh preparation, dispatch only its sealed bodies
through the shared executor and never retry/resume consumed authorizations.
No production enablement, SDK/MCP/assistant update or complete SQL/write/read
integration follows automatically from this diagnostic.
