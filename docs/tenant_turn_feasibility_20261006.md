# Current-turn feasibility: NO-GO for the wait-only path

Date: 2026-10-06. Backend baseline: `f166149`.
Scope: one authorized development experiment, not a blind holdout or pilot
acceptance pass. No production writes, live flags, prompts, validators, SDK,
assistant, deployment or acceptance targets changed.

## Decision

Do not implement or activate a short worker-wait path as the proposed remedy.
The tested configuration did not provide usable ordinary-recall context, and
provider latency alone exceeded the proposed 750 ms foreground target.
Moving dispatch earlier cannot eliminate those observed classifier costs or
turn invalid replies into backend-valid decisions.

This rules out the proposed wait-only remedy for this configuration. It does
not establish that every provider/model or a narrower MemoryOS pilot is
infeasible. Do not relax the criteria, repair outputs, change models or rerun
the same batch to manufacture a pass.

## Protocol

- Fresh synthetic tenants/users in a dedicated local PostgreSQL database and
  Redis instance. Existing customer/local-stack databases were not used.
- 30 ordinary questions: 10 English, 10 Hindi, 10 Hinglish. Three corrections
  and three uncertainty cases, one of each per language. These are public
  development examples, not independently human-labeled semantic gold data.
- Real authenticated HTTP `prepare-turn` admission, production runtime worker,
  existing intent schema/evidence binding, transactional currentness and SQL
  retrieval guard. Each case starts with a controlled global C++ preference.
- OpenAI `gpt-4o-mini` was the existing local configured alias. No model
  substitution; temperature 0 and the worker's 8,192 output-token ceiling.
  The returned API snapshot identifier was not retained by the existing
  response adapter; do not infer an exact snapshot from the alias.
- One provider only, no provider fallback/retries or output/prompt repair.
  Circuit/concurrency state was isolated from live Redis. Controlled broker,
  embedding and cache delivery; controlled vector nominations followed by real
  SQL eligibility. No answer-generation model or signed-in browser manipulation.
- Start the existing worker immediately after admission in the harness, then
  read after completion. No production endpoint wait path was added.
  Sum admission + worker + read as an optimistic local candidate duration:
  real queue, live vector/embedding, network-to-backend and browser costs are
  not measured. This is not an ECS deployment, load test or production SLO.
- $0.30 ceiling, conservative per-call reservations, no secrets persisted.
  The one-time marker prevents rerunning the same artifact path.

## Observed outcomes

| Group | Cases | Rejected at intent validation | Expected diagnostic outcome |
| --- | ---: | ---: | ---: |
| Ordinary recall | 30 | 29 | 0 |
| Correction | 3 | 2 | 1 |
| Uncertainty | 3 | 3 | 0 |

There were 37 completed provider calls with reported usage across 36 cases.
The extra call came from the existing nonpreference/fact planning lane, not a
second implementation of the intent classifier.

All initial responses withheld context with a committed processing guard. The
one ordinary case that passed initial intent validation classified the question
as a nonpreference; its completed worker result retained a broad unavailable
hold and returned no current context. Its 6,285.95 ms local candidate duration
is one observation, NOT an ordinary-recall p95.

The Hindi correction produced an authorized Python replacement and one current
retrieved preference at a local candidate duration of 3,651.36 ms. The other
corrections and all uncertainty cases did not produce valid classifier output;
they are not counted as semantic or usability passes merely because the initial
guard stayed safe.

Usable ordinary context within 750 ms: **0/30**. Usable expected ordinary context
even after successful worker completion: **0/30**.

Provider wall-time across 37 calls: median **2,743.51 ms**, nearest-rank sample
p95 **4,063.40 ms**. These include the extra fact-lane call and are not a
classifier-only 30-sample p95, full HTTP p95, first-token measurement or
production p99 estimate.

## What is and is not established

The decisive timing result does not depend on treating invalid replies as
successful processing. The tested provider calls themselves exceed the proposed
foreground budget. A wait timeout would preserve safety but not useful recall.

33 cases reported `PreferenceAdmissionUnavailable("intent_response_invalid")`.
The classifier's catch covers both schema parsing and evidence/binding failures.
This harness retained usage/timing and aggregate errors, but not raw provider
responses or chained validation exceptions. Consequently it cannot establish
the precise cause of each rejection or attribute every failure to model meaning
versus a validator constraint. Do not claim that root cause is fully diagnosed.
The accepted ordinary nonpreference finding is directly observed; the reasons
for the other rejected outputs remain unobserved.

The provider-free preflight using empty arrays successfully applied a no-change
receipt and returned the current C++ context. This verifies that the harness
can exercise that path; it does not substitute for a real-model result.

Against [the frozen pilot checklist](tenant_pilot_acceptance.md), P1 useful
ordinary recall and P9 the proposed latency/availability target do not pass in
this diagnostic. P2-P8 were not fully evaluated: multilingual samples, local
SQL checks and safe initial holds do not prove the complete customer chat,
identity lifecycle, attack handling or recovery acceptance rows.

Next work, if authorized, must isolate the rejected output and validator cause
on a single fresh diagnostic case before changing behavior. Preserve the failed
batch. Do not repeat all 36 requests, add a regex escape hatch, release unknown
guards, or broaden architecture in response to this result. A production pilot
remains NO-GO for current-turn preparation; existing deployed behavior is unchanged.

## Cost and artifact integrity

Reported usage: 49,899 input tokens and 2,720 output tokens. At the verified
standard rates of $0.15/M input and $0.60/M output, the conservative uncached
token cost is **$0.00911685** (about $0.00912). All 37 calls settled to reported
usage; no unknown-charge reservations remain. This is an estimate, not an
invoice, and omits any cache discount. Rates were checked against
[official OpenAI model documentation](https://developers.openai.com/api/docs/models/gpt-4o-mini).

Ignored local artifacts: `artifacts/turn-feasibility-20261006/experiment.py`,
`results.json`, and `started.once`. Only synthetic outcomes, not credentials,
are retained. SHA-256:

- Cases: `0255720b35bb794dc9c917f3a9925498588028f4c303afc24ab47371efa3c572`
- Experiment: `db324a8a043ff8a4b11bd4f36abfd920d656985bcf772b6bffd2121f7f636dcb`
- Results: `a818eaa526aac227561a6a448771c8656ad100f42bc1cfa880064ff3cf88c0b0`

Disposable test containers/data were removed after completion. The preserved
JSON outcome artifact and this report remain available for review.
