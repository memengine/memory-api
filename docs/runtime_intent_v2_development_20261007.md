# V2 development evaluation: NO-GO

Date: 2026-10-07. Evaluated backend: `9a42514`.
Decision: keep V2 opt-in and unpromoted. The approved review-first policy remains
useful, but this model/configuration did not meet the frozen diagnostic expectations.
Do not enable production or claim pilot readiness from this run.

## What ran

One explicitly approved development run, $0.30 ceiling, 21 unique classifier calls.
The configured alias was OpenAI `gpt-4o-mini`; every returned API model identifier
was `gpt-4o-mini-2024-07-18`. There was no model substitution, provider fallback,
output repair, prompt tuning or paid retry. OpenAI returned responses and usage
for all calls: this was not an API outage.

The unchanged public development inventory has seven cases each in English,
Hindi and Hinglish, across seven families. Its expectations are AI-authored
diagnostics, not independent human gold, calibration data or a blind holdout.
Its normalized UTF-8 SHA-256 is
`39914b6bb61805bd89e59f6bac8b87df2d7b28c7c99141ab8ee6105f10c77f1f`.

Each request used a detached snapshot captured from a fresh owned synthetic
PostgreSQL fixture with a global C++ default at authority 20. The backend-supplied
catalog also offered an absent Cedar project scope and answer-detail property;
their presence was not evidence that the user requested them. Actual transcript
roles were `user`, source kind `client_assertion`. No customer records were used.
Fixture seed writes and embeddings were controlled; the model's replies were
**not applied to memory**. SQL fixtures were cleaned after capture.

The production `classify_intents(..., contract_version=2)`, production OpenAI
transport, canonical parser and review-first binder were used. Requests retained
temperature 0 and the 8,192 completion-token ceiling. Before paid execution, all
21 frozen snapshots reproduced byte-identical request bodies. The maximum total
conservative reservation was $0.13891905, below the authorized ceiling. Raw API
bodies, returned model, usage, timing and chained validation causes were saved
before downstream parsing. No authorization headers or credentials were saved.

The capture script is a one-off ignored diagnostic, not another production
classifier or reusable benchmark framework. A Windows stdout encoding error
stopped printing after two completed calls. Both replies and usage were already
durable. After fixing stdout encoding, only the 19 uncalled entries continued
against the same frozen requests. The completed calls were not repeated.

## Results

| Family | Cases | Diagnostic matches |
| --- | ---: | ---: |
| Recall | 3 | 0 |
| Explicit reaffirmation | 3 | 3 |
| Decided correction | 3 | 3 |
| Uncertainty | 3 | 0 |
| Mixed recall/update/fact | 3 | 0 |
| One-off request | 3 | 0 |
| Question-shaped lasting update | 3 | 1 |
| Total | 21 | 7 |

Language matches: English 3/7, Hindi 2/7, Hinglish 2/7. These are small
diagnostic counts, not precision/recall/F1 or general language guarantees.
Nine cases were rejected by parsing/evidence binding. Five more passed those
checks but had semantically wrong classifications/scopes. Twelve cases were
backend-valid; only seven matched the diagnostic expectations. A valid schema or
eligible source span is not independent proof of meaning.

All backend-valid preference value proposals became pending reviews, with zero
automatic preference declarations. This verifies the new binding policy on these
responses, not end-to-end safety of their fact lane, confirmation or retrieval.

## Concrete failure causes

1. **Recall was misrouted into the fact lane in all three languages.**
   `recall-en` emitted fact content `C++` even though that value was absent from
   the user's question. Hindi emitted a generated description of the user asking
   a question; Hinglish inserted the stored C++ value into generated wording.
   All three failed `fact requires verbatim evidence`. The previous v1 recall
   error manifested as reaffirmation; v2's structural restrictions did not remove
   the semantic error and these responses expressed it through nonpreferences.
   The different runs are not a paired accuracy comparison.

2. **Five cases exposed the content/quote contract mismatch.**
   The canonical `NonPreferenceFinding` accepts content contained within its
   quote, while the final extractive evidence validator requires content equal
   to the verified source clause (or exact joined clauses). `unsure-en`,
   `mixed-hi` and all three one-off cases supplied a content subset with a
   broader quote and failed `candidate_source_mismatch`. This explains the exact
   rejection; it does not make their semantics correct. The uncertainty example
   discarded the unresolved qualifier, and one-off requests were incorrectly
   treated as facts/goals. Do not loosen the evidence gate to manufacture passes.

3. **One mixed response fabricated quotation punctuation.**
   `mixed-en` added a period to the preference quote where the transcript had a
   comma and continuation. The exact source check returned `quote_not_found`.
   It also classified the recall question as a fact instead of omitting it.

4. **Two uncertainty responses were verbatim but semantically wrong.**
   Hindi and Hinglish uncertainty went into `nonpreferences` as facts, with no
   `withhold` choice. Their exact text passed source checks. The classifier and
   binder alone therefore cannot establish that the old default would stay
   withheld by the complete processing/retrieval path. No such result is claimed.

5. **Valid offered scopes/properties were selected without user evidence.**
   Hindi/Hinglish question-shaped updates selected Cedar rather than global even
   though the user never mentioned Cedar. `mixed-hinglish` likewise selected
   Cedar and classified the exam date as `answer.detail`, while retaining the
   recall question as a fact. These passed representation/binding checks but
   failed the diagnostics. `mixed-hi` exhibited similar invented detail/scope
   classification, in addition to its source mismatch. Offering an identity does
   not prove the user intended it. Review-first prevents immediate preference
   activation, but wrong review cards still fail usability/meaning requirements.

## Timing and cost

Measured provider HTTP wall-time: median 2,015.91 ms; nearest-rank sample p95
2,490.44 ms; range 1,659.49–3,185.15 ms. All 21 calls exceeded 750 ms. These
include local HTTP/client/network time and are not foreground MemoryOS latency,
first-token latency, production p99, queue behavior or load-capacity measurements.
This supplies no evidence for a wait-only foreground remedy.

Reported usage: 38,714 input tokens and 1,321 output tokens. Standard-rate
token estimate: **$0.0065997**, with zero unknown-charge calls. The accounting
uses $0.15/M input and $0.60/M output, conservatively ignoring any cached-input
discount; it is not an invoice. Rates were verified against the
[official model page](https://developers.openai.com/api/docs/models/gpt-4o-mini).

## Evidence and boundaries

Ignored local directory: `artifacts/recall-operations-v2-20261007/`.

- `frozen.json`: complete snapshots, requests and diagnostic expectations;
  SHA-256 `ac394764841fab73d4e193bdb9f939574b0a011af639fde9a2d9528ae72209ec`.
- `results.json`: all 21 raw responses, usage and outcomes;
  SHA-256 `f3472a5519ac62d8f93e8fbbcac49c8c92d668417c1e642c3cb778c201f47ccd`.
- `summary.json`: aggregate results;
  SHA-256 `60d2a30c78927792fdebc2373f43672cdddf46f6d1c19a691c11f5fcf6ec42fa`.
- Initial evaluator SHA-256 before the stdout repair:
  `fdd4f9bff40235413d39754dae5c1a3597c95a2043c362d022adf268007934d0`.
  Final evaluator SHA-256:
  `9f1f3a15713d60abd03b8de540b53ac148979209ae99abdc0898885485fef8e0`.
- Exclusive `started.once` and `resume_uncalled.once` markers preserve the
  consumed run. Do not restart or regenerate this experiment to tune its outcome.

No production source, validators, SDK code, assistant/MCP/tenant UI, model
configuration, live flags, deployment or holdout changed. Documentation records
the result; no activation is authorized. The frozen pilot P1–P9 and previous
latency NO-GO remain in force.

The next decision should separate model understanding from evidence-contract
alignment using these saved failures, not launch another prompt/edit/test loop.
A stronger-model paired diagnostic on the unchanged inputs can test a model
capacity hypothesis, but requires separate model/spend approval and would not by
itself prove that the fact lane, scope semantics or foreground architecture is
ready. No new run, repair or production change is authorized by this report.
