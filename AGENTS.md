# MemoryO / MemoryOS: operating guide and release roadmap

Updated: 2026-10-11 (Asia/Calcutta). Applies to this backend repository.
MemoryO is the product name in the owner's operating instructions; MemoryOS
remains the existing repository/API name. No rename is authorized.

This is the single release roadmap. The documents below specify contracts and
evidence; they do not create competing roadmaps. Resume the next unfinished task
instead of repeating the historical audit.

## Mission, customers and boundaries

Build a governance and control layer for AI memory and state: owned claims,
explicit scope, provenance, authority, version-bound change and accountable
retrieval. Support agent applications as well as conversational applications.
Do not reposition the product as a vector database or episodic memory service.

Initial customer hypothesis: teams whose agent or conversational application has
multiple components writing user preferences, and whose incorrect replacements,
stale commands or uncertain operation outcomes cause actual operational work.
Buyer hypothesis: the engineering lead responsible for agent reliability.
Reason to adopt: fewer incorrect durable updates and less effort establishing
which preference is current, who supplied it and whether a change applied.
These are hypotheses, not customer-validated demand or measured benefits.

The selected engineering scope is the already approved committed-state explicit
preference pilot. An application may supply preferences to its agents, but this
does not establish authenticated per-agent writes or arbitrary shared task state.
Agent-bound and service-event typed writes are currently rejected. Do not work
around those restrictions or advertise multi-agent arbitration as supported.
Validate usefulness to agent teams before expanding beyond this scope.

Non-goals for the first release: automatic natural-language interpretation,
generic facts or task-state arbitration, full conversation recall, universal
prompt-injection detection, cross-store governance adapters and control over
downstream model/tool behavior. A governance decision cannot force an agent to
obey it. Source evidence does not prove human intent or human authorship.

## Authoritative contracts and evidence

- Binding pilot scope, samples, thresholds, ownership and one-evaluation stop
  rule: `docs/EXPLICIT_OPERATIONS_PILOT_FREEZE_20261008.md` (Oct 9 amendment).
- Existing API declaration/read contract and limitations:
  `docs/explicit_preference_contract.md`.
- Authoritative request types: `api/schemas/preferences.py` and
  `api/schemas/requests.py`; observer wrapper types:
  `benchmarks/internal/explicit_operations_pilot.py`.
- Observer commands and limitations: `docs/EXPLICIT_OPERATIONS_OBSERVER.md`.
- Historical semantic stop/evaluation record:
  `docs/NEXT_SESSION_HANDOFF_20261007.md`. Its original proposed P1-P9 scope
  does not override the approved narrower contract.

The two explicit-operation docs are ignored by Git under current rules, and the
observer/schema exports are untracked. They are working artifacts, not committed
or released capabilities. Do not change ignore rules or stage unrelated work.

## Verified baseline

Source inspected on 2026-10-11: HEAD
`4477b839f428f53f787c5a638a7a480dd741e820`. Source verification is not runtime
qualification. No tests, provider calls, service startup or evaluation performed
when creating this roadmap. The later approved synthetic replay attempt below
initially was inconclusive. The separately approved socket-free diagnostic
reproduced shared /add cache-hit metadata misattribution; it is not acceptance
evidence or deployed/end-to-end qualification.

| Item | Status | Evidence and what it establishes |
| --- | --- | --- |
| Current HEAD and working-tree inspection | DONE | `git rev-parse HEAD`, `git status --short`, 2026-10-11. Existing edits/untracked work preserved. |
| Committed-state scope decision | DONE | Approved Oct 9 amendment in frozen contract: one declaration/request; queued means scheduled; old committed value may remain readable before replacement commits. |
| Typed preference governance components | PENDING | `preference_claim_service.py:155-240` validates declared evidence, identity and target/version; reserves/locks claims and checks authority. Components exist; full independent end-to-end acceptance is not demonstrated. |
| Immediate new/pending visibility | DEFERRED | Explicit contract:347-349 disclaims read-after-write. Freeze records the guard-path mismatch. Excluded by approved amendment; do not reopen without new evidence or a customer requirement. |
| Retained replay guarantee | BLOCKED | Durable admission: 18 isolated PostgreSQL/Redis + 59 focused unit checks PASS. Separate approved one-run postcommit/predispatch diagnostic reproduced a 30 s recovery failure: queued at 30.265 s, zero dispatches/mutations; receipt retained, no duplicate admission. Watchdog unchanged (60 s grace/120 s schedule). Full P8 cannot be claimed. |
| Thin observer and schema exports | DONE | Existing observer/schema artifacts and recorded successful schema exports. This establishes an interface only; `pilot_qualified=False` and coverage/failure-evidence gaps remain. |
| Runtime, application and private-data qualification | BLOCKED | No verified local candidate/environment or compatible unused independent dataset established. SDK/application identity, status delivery and measured latency unverified. |
| Natural-language V2 qualification | DEFERRED | Oct 7 diagnostic: 7/21 at `9a42514`, NO-GO. Keep V2 disabled; not an explicit-operation acceptance result. |
| Phase 3A proposal confirmation | DEFERRED | Consumed v5: 47/50 at `f5aa49e`, different path. Supporting history only; no rerun or pilot qualification. |
| AWS shutdown | DONE | Owner's final targeted destroy check: "No changes. No objects need to be destroyed." Accepted evidence, not a new infrastructure inspection. |
| Product usefulness and demand | PENDING | No independently measured customer benefit, named committed design partner or willingness to pay established. |

Existing tracked modifications at inspection: `.gitignore`, semantic-development
`review-a.blank.json`, `review-b.blank.json`, and
`benchmarks/internal/preference_exploratory_run.py`. Do not open private cases or
labels. Preserve all unrelated tracked/untracked artifacts.

## Release scope and acceptance

Keep the frozen scope: explicit store, exact owned/versioned supersede or choose,
property/scope-specific committed preference read with labelled global fallback,
and owned status/history inspection. Exactly one declaration per request is an
application/pilot restriction; the backend declaration helper accepts 1..16.

The customer application must securely bind tenant/user, maintain stable external
identity across sessions, supply the operation/value/scope/evidence/target/version,
and distinguish scheduled, rejected and verified applied outcomes. A completed
job or matching pre-existing value alone does not prove this request applied.
Later authorized successors are allowed. No inferred intent or immediate
read-after-queue promise.

Technical release gate is every row of the existing frozen contract, unchanged:
P2/P4/P5 30/30 each; P6/P7/P8 60/60 each; P1/P9 60/60 correct reads, >=58/60 useful,
foreground nearest-rank p95 <=750 ms, first rendered result/status p95 <=4 s,
terminal outcomes <=30 s. Any safety violation fails. Reviewer freezes overlap,
controls, workload and expected outcomes before the one evaluation. These are
pilot scenario coverage, not general-release reliability estimates.

Freeze actual API/worker/application artifacts and settings, not just HEAD.
Verification must cover authenticated input -> admission -> worker -> committed
state/provenance -> scoped retrieval -> application-visible outcome. Preserve
denials, errors and slow samples. Controlled local evidence cannot establish cloud
deployment, real embedding-provider reliability, production scale or production
latency. Deployment/publication and any provider calls require separate approval.

Proposed product gate, not yet customer-validated: interview three relevant teams;
at least one supplies a concrete recurring failure, accepts the exact boundaries
and commits an integration owner to a bounded pilot. If none does, pause feature
expansion and decide positioning. Proposed pilot adoption check: within two weeks,
the partner operates the integration repeatedly in its workflow, supplies one
documented benefit relative to its current method, and explicitly chooses whether
to continue. Positive reactions or benchmark scores alone are insufficient.

## One prioritized roadmap

These are release work items, not new semantic phases or architecture.

| Order | Deliverable | Status | Dependencies and definition of done |
| --- | --- | --- | --- |
| 0 | Establish this operating guide and bounded replay repair specification | DONE | Current source/contract inspection; this file records baseline, exclusions, evidence and next task. Documentation only. |
| 1a | Reproduce suspected shared /add replay safety mismatch | DONE | Separately approved Oct 11 socket-free run, exit 0: one changed owner/content request returned seeded user A job ID as queued. Before/after cache equal; zero forbidden dependency/network attempts. Evidence below. Limited route-level reproduction, not typed-path or persisted-state qualification. |
| 1b | Repair demonstrated cache-hit owner/payload mismatch | DONE | Separate repair approval; working-tree router/service/helper patch. 27 focused regression checks + 2 existing test bodies PASS; old unbound entries fail closed. Evidence: repair report below. This is not complete P8 qualification. |
| 1c | Specify retained concurrent-admission/cache-loss guarantee | DONE | Read-only source-backed proposal in `docs/EXPLICIT_REPLAY_ADMISSION_SPEC_20261011.md`: existing-job binding/index plus guarded admission. Receipt retention and 30 s recovery limits explicit; no implementation or tests performed. |
| 1d | Decide durable receipt lifetime and admission change | DONE | Owner approved at least 24 h after acceptance and longer while nonterminal, subject to explicit deletion; protection ends with job deletion. Existing lifecycle retains job rows without automatic purge. No new receipt table or deletion-policy change. |
| 1e | Implement and verify bounded durable admission | DONE | Existing job columns, SQL tenant/key uniqueness, bounded transactional guard and current SQL permission checks. 18 isolated integration + 59 focused unit checks PASS. Report: `benchmarks/internal/reports/durable-add-replay-repair-20261011.md`. Owner-approved commit `4bf8884737ef58416bb34b9d831b0836333d21c0` recorded as current source pin; runtime/schema freeze pending. No dispatch/watchdog changes or full P8 qualification. |
| 1f | One bounded dispatch recovery diagnostic | DONE; requirement FAIL | Approved synthetic exception after receipt commit, before initial dispatch. Restored dependency; existing watchdog cycles at restoration and 30.25 s find zero eligible jobs. At 30.265 s original job remains queued; one receipt/source, zero memories. `benchmarks/internal/reports/dispatch-recovery-diagnostic-20261011.json`. No repair authorized; no rerun. |
| 1g | Specify bounded same-job dispatch recovery | DONE | Owner approved specification and then exact implementation scope: existing rows/delivery ID/worker claim, independent publication and distinct 5 s task in existing Beat. No new table/migration; watchdog 60/120 unchanged. |
| 1h | Implement and verify pre-worker dispatch recovery | DONE; full P8 still BLOCKED | 61 unit + 18 admission + 19 non-scheduled recovery + 1 scheduled local check PASS. Real Beat/Redis/controlled worker completed one API-killed receipt in 1.250 s; duplicate broker delivery ignored, one mutation. Original failed diagnostic preserved; first timing harness invalid, second valid. `benchmarks/internal/reports/dispatch-recovery-repair-20261011.md`. Owner approved commit and source repin. No worker-started crash/deployed qualification. |
| Parallel | Validate initial use case with relevant agent teams | PENDING | Owner conducts or expressly authorizes outreach. Three interviews and named partner gate above; no invented contacts or unsolicited messages. |
| 2 | Establish a usable controlled local vertical slice | DONE for approved store/read/replay slice | Owner approved scoped execution. Pinned `076e0ed` export; real HTTP auth/routes/asyncpg/Redis/worker, controlled embedding. One explicit C++ store committed; fresh HTTP client with stable application owner/scoped read selected same memory; replay unchanged, queue slot released. Read 542.532 ms (one sample). Global applicability in provenance, not rendered prompt. Replacement/choice, SDK, production startup and broader qualification not established. `benchmarks/internal/reports/explicit-local-slice-20261011.md`. |
| 3 | Close only evaluation-interface gaps needed by frozen contract | DONE for approved observer v2 patch; qualification pending | Existing observer extended: history/reviews, fresh clients, receipt/source/outbox/guard projections, true deadlines, declared transient GET polling, after-state/uncertainty stops, bound operator evidence/runtime hashes/schema. 26 unit + 1 real SQL projection PASS; 3 schemas exported. No backend/dataset/evaluation changes. Report: `benchmarks/internal/reports/explicit-observer-repair-20261011.md`. Owner approved commit/source-observer freeze; runtime proofs/data NOT ESTABLISHED. |
| 4 | Freeze candidate and run one independent acceptance evaluation | BLOCKED | Items 1-3, partner scope acceptance, private unused human-adjudicated data and separate execution approval. Receive only hashes, coverage and aggregate results; every frozen row accounted for. |
| 5 | Decide and release the bounded pilot | BLOCKED | Frozen outcome: ship eligible pilot, narrow to independently evidenced useful subset, or stop. No ordinary patch-and-rerun. Deployment remains a separately approved action. |

## Proposed date and critical path

Planning target: **2026-11-02 for a local design-partner pilot**, conditional on
approval and partner participation. This is not a production launch commitment.
Engineering effort has not been measured; planning assumes one focused engineer,
no architecture expansion, roughly one week for replay/slice work and one week
for bounded observation/verification, with customer discovery in parallel.
The remaining time is for owner-controlled freezing and adjudication/evaluation.
Private-data effort is unknown; do not ask the owner to author data before the
candidate and interface are ready.

Critical path: safety-repair approval -> reproduced replay defect -> verified
repair -> usable local slice -> complete observation -> independent freeze/run ->
release decision. Customer fit is a parallel gate. Reassess this date after item 1;
if its solution requires a migration/architecture decision or no partner accepts
the scope, explicitly revise the date or scope. Never silently extend it.

## Blockers, uncertainties and deferred work

Release blockers: unqualified recovery/production replay; incomplete candidate/runtime/application
verification; observer gaps; frozen isolation/authority/concurrency/recovery
evidence; no established independent data; no validated design-partner use case.
Shared route cache-hit metadata misattribution is now reproduced. No proven
job-access exploit, persisted unauthorized memory mutation or full end-to-end
typed-operation reproduction is claimed.

Uncertain: old runtime holds can affect reads even with V2 off; full P8
crash/recovery behavior; meaningful read latency; retained source/history
availability after lifecycle actions; actual SDK/application alignment. Resolve
only evidence necessary for the selected release. Tenant/source isolation and
sensitive-data/lifecycle controls need scoped evidence; do not claim them from
the presence of endpoints.

Deferred backlog: per-agent/service-source explicit declarations; generic shared
task state; external memory-store adapters; richer authority policies; customer
operation receipts beyond existing inspectable status; bounded reversal semantics;
NL interpretation with separate held-out evaluation; episodic retrieval; dashboards
and SDK convenience work beyond the minimum integration. Prioritize only against
observed customer need. No item grants implementation permission.

## Decision log

- Oct 7: V2 diagnostic NO-GO; automatic interpretation deferred. No semantic
  patch loop and no proposed P1-P9 automatic scope presented as approved.
- Oct 9: approved one-declaration committed-state preference pilot; queue
  acknowledgement is not application. Isolation, authority and replay retained.
- Oct 9: AWS shutdown accepted; old deployment is historical evidence only.
- Oct 9: observer/schema preparation is implementation evidence, not qualification;
  compatible unused private acceptance data remains NOT ESTABLISHED.
- Oct 11: owner's CTO/product instructions authorize this root operating guide.
  Agent-first mission retained; current preference scope is a bounded candidate,
  not a general agent governance capability or proof of demand.
- Oct 11: inspect/source-specify completed; preserve the frozen reproduced-safety
  repair gate rather than infer that general operating instructions authorize
  changes to all previously frozen boundaries.
- Oct 11: owner approved one isolated synthetic reproduction only. First launch
  failed before the handler because the network guard blocked Windows asyncio's
  local socketpair. After a harness-only startup correction, the process produced
  no result/artifact and was interrupted. Outcome INCONCLUSIVE; neither violation
  nor safe refusal demonstrated. No backend repair or broader investigation.
- Oct 11: owner separately approved the same diagnostic with phase markers and
  a socket-free executor. One completed handler invocation returned user A's job
  metadata for changed user B/content. Scope of reproduction is the shared /add
  cache-hit boundary only. Item 1a DONE; item 1b remains BLOCKED for separate
  repair approval. No new release requirements or evaluation rerun introduced.
- Oct 11: owner approved bounded cache-binding repair. Both cached-return paths
  now compare tenant/owner/request binding; cached acceptance stores it. Exact
  replay succeeds; changed or unverifiable entries refuse. Legacy unbound entries
  deliberately fail closed. 27 focused checks and 2 existing test bodies PASS.
  No durable/concurrency P8 guarantee earned; V2/flags/frozen contract unchanged.
- Oct 11: owner authorized the bounded read-only follow-up specification. Recommend
  durable request-key/binding on existing jobs, tenant/key uniqueness and guarded
  admission. No implementation authorized. Source shows 60 s queued grace and
  120 s watchdog schedule; these cannot alone establish retained 30 s recovery.
  Receipt horizon/deletion semantics must be decided before storage design is
  finalized. This records existing P8 dependencies, not new launch scope.

## Current checkpoint: bounded durable admission completed

Status: approved durable-admission repair committed; source pin
`4bf8884737ef58416bb34b9d831b0836333d21c0`. The separately approved single recovery
diagnostic completed and reproduced a retained 30 s recovery requirement failure.
Receipt uniqueness held; no unauthorized/duplicate mutation occurred. See
`benchmarks/internal/reports/dispatch-recovery-diagnostic-20261011.md` and JSON.
Owner subsequently approved the specification and exact implementation. The
pre-worker dispatch repair is complete in the working tree, with 99 distinct
focused checks passed across recorded runs. Real scheduled recovery completed
in 1.250 s; the same-ID broker duplicate added no mutation. Original failure
evidence remains immutable; first timing attempt was invalid because Beat failed
at harness startup, separately preserved. See
`benchmarks/internal/reports/dispatch-recovery-repair-20261011.md` and the approved
spec. Owner approved commit/source repin and then scoped local-slice execution.
The bounded store/reconnect/scoped-read/replay slice passed through real HTTP
authentication and worker storage. Next proposed deliverable: bounded read-only
observer/interface readiness review against this demonstrated path and the
existing frozen contract. Owner approved the review and observer-only patch, now
complete with 27 focused checks. Owner approved observer commit/source freeze.
Observer raw SHA-256 `1820497f88b1758279b299f40283f58e738f23985011e43e51c71ebb2ba0c87f`;
source candidate commit recorded in the candidate document after commit. Runtime
artifact/settings verification and unused independent data remain pending; no
private cases/labels inspected, no acceptance execution or deployment authorized.
No further repair, fault injection, dataset or acceptance run authorized. Original
watchdog 60/120 and worker processing limits unchanged. Full runtime/application
freeze, worker-started crash behavior and full P8 remain unqualified.

Slice evidence: `benchmarks/internal/reports/explicit-local-slice-20261011-run3.json`
and `explicit-local-slice-20261011-review.json`. First two attempts failed bootstrap
prerequisites (no child DATABASE_URL; missing synthetic ADMIN_SECRET), before
authenticated writes; preserved, not candidate failures. Third passed with real
bcrypt-backed synthetic key, queued/processing/completed API observations,
correlated created memory/source/provenance, fresh-client scoped read and original
job replay. One job/source/memory/revision/outbox and two audit rows after reads;
replay counts unchanged; reservation depth zero/member absent. Export matches
commit text with CRLF-only conversion. SQL uses pinned metadata, not historical
migration chain. Test bootstrap replaces production lifespan/region wiring;
child-only connection/admin settings and in-memory flags. No backend changes,
provider calls/private evaluation/new acceptance cases. Disposable runtime removed.
Global scope visible in structured provenance but absent from prompt annotation;
full P5/application label consumption remains unqualified. Dataset NOT ESTABLISHED.

Historical inconclusive execution record:
`benchmarks/internal/reports/replay-safety-reproduction-20261011.json`.
Completed evidence:
`benchmarks/internal/reports/replay-safety-reproduction-20261011-socketfree.json`.
Completed evidence SHA-256:
`b6b5ac59ed8a5e9cb4e1dd31e2146e30428ca547a41abb36ecba5d8479f670df`.
Harness: `benchmarks/internal/replay_safety_reproduction_20261011.py`.
Completed command: `.venv\Scripts\python.exe -B -m benchmarks.internal.replay_safety_reproduction_20261011 benchmarks\internal\reports\replay-safety-reproduction-20261011-socketfree.json`.
Exit 0; markers establish imports, cache seed, exactly one handler invocation,
response and evidence completion. No event loop or external work was scheduled.

Seed: tenant `00000000-0000-4000-8000-000000000011`, owner `synthetic-user-a`,
queued job `00000000-0000-4000-8000-000000000012`, key
`synthetic-replay-20261011-one`. Replay: same tenant/key, owner
`synthetic-user-b`, content changed from "Synthetic original: blue." to
"Synthetic changed: green." Expected refusal without earlier job metadata;
observed queued response containing the seeded user A job ID. Cache before/after
SHA-256 both `a225213a13337d45227d26ef3de71e8e19a8e41119fef25be833f06d6c36af1a`.
One cache seed/set and one get; zero forbidden dependency or network attempts.
The synthetic ledger is an in-memory fixture, not a database-state observation.

Limits: direct handler call, supplied tenant identity, ordinary schema-valid /add
payload without preferences, seeded prior cache rather than initial admission,
fake Redis transport and no worker/database. Returning another supplied user's
job metadata is the demonstrated violation. No raw memory disclosure, successful
job access or persisted mutation established. Changing owner and content together
does not independently isolate a same-owner payload-only failure. The typed route
shares this branch when enabled, but was not runtime-exercised; no flags changed.

Single next task: decide receipt lifetime/post-deletion semantics and approve or
reject the bounded existing-job durable-admission proposal in
`docs/EXPLICIT_REPLAY_ADMISSION_SPEC_20261011.md`. No further implementation
authorized. Nov 2 remains conditional; schema/lifecycle and recovery decisions
now identified on the critical path, so the date is not an earned launch promise.

Invariant: reusing the same request identifier for changed owner or changed
declaration must refuse without disclosing another owner's result or mutating
state; exact authorized replay returns the same logical operation. Concurrent
exact attempts cannot cause multiple logical mutations. Cache expiry/outage
must have a defined safe behavior rather than silently earning a durable promise.

Root cause established by source: both router and service use cached job metadata
without binding/comparing incoming owner and payload. `api/db/cache.py:455-484`
stores an expiring response (default 86400 seconds), not an atomic request
reservation. Existing `test_memory_service.py:294-360` tests tenant scoping, not
changed owner/payload refusal. `api/db/models.py:2786` defines ExtractionJob;
its source-event uniqueness does not establish typed-request uniqueness.
Do not prescribe a cache-only fingerprint patch as a complete P8 repair.

Approved implemented repair for the demonstrated cache-hit defect:
bind cached acceptance to tenant, effective owner and a versioned canonical
request digest, including messages, declaration/scope/target/version and other
ingestion-relevant fields. Compare that binding before any router/service cached
return. A mismatch must refuse without the prior job ID; an unbound legacy entry
must not be treated as a verified exact explicit-operation replay. Do not put the
digest into a new key and silently admit changed requests as fresh operations.
Use one comparison helper at both early-return boundaries and store the binding
with the existing cached acceptance. Assess generic/legacy compatibility before
changing that path. No new endpoint, semantic rule or governance-policy change.

This repair addresses the observed metadata mismatch only. Durable uniqueness,
cache expiry/outage and concurrent first admissions remain separate retained P8
questions; a post-admission cache binding alone does not establish at-most-once
admission. Any required schema/architecture change needs its own concrete approval.
Synthetic checks now distinguish exact replay, changed owner only, changed
payload only and unbound/invalid entries at both boundaries. They are regression
evidence, never independent pilot qualification.

Repair evidence: `benchmarks/internal/reports/memory-add-replay-binding-repair-20261011.md`.
New helper: `api/services/memory_add_replay.py`; router/service changes plus
`tests/unit/test_memory_add_replay_binding.py` and two adjusted existing fixtures.
Command: `.venv\Scripts\python.exe -B -m pytest tests/unit/test_memory_add_replay_binding.py -q -p no:cacheprovider -p no:asyncio`;
final result 27 passed in 5.01 s. Two existing tenant-scoping test bodies also
passed through a socket-free direct coroutine executor. Scoped `git diff --check`
passed. No new flags/settings, provider calls, deployment or private evaluation.
Admission/dispatch counters in controlled tests are not persisted-state evidence.

Owner subsequently approved the bounded existing-job durable repair. The SQL
receipt retains a key hash and versioned binding independently of raw-payload
redaction; current cleanup never automatically deletes jobs. Explicit job/owner/
tenant deletion ends protection, and a later authorized request may be admitted
again. No tombstone, permanent duplicate prevention or deletion-policy change.
SQL uniqueness and a transaction guard precede queue reservation. SQL receipt
lookup rechecks tenant, API-key permissions and owner availability; cache misses,
expiry and outage cannot invent a second eligible admission. Legacy unbound jobs
are not backfilled. Service-event/agent/MCP/prepared-turn paths retain their own
contracts; no broader durable guarantee is claimed for them.

Evidence: `benchmarks/internal/reports/durable-add-replay-repair-20261011.md`.
18 synthetic isolated PostgreSQL/Redis checks PASS (7.26 s), including five-way
normal and unique-index fallback contention, exact replay, owner/payload conflicts,
current SQL revocation, raw redaction, receipt/cascade deletion and duplicate worker
delivery while processing and after completion. 59 focused unit checks PASS
(15.80 s). Controlled real worker storage retained one memory with unchanged
revision/audit/outbox counts on duplicate deliveries; no worker code changed.
These are regression evidence, not independent acceptance or deployed behavior.
Watchdog 60 s grace/120 s schedule remain unchanged; P8 still requires 30 s
recovery. Dispatch-crash recovery, live middleware/SDK and full pilot remain
unqualified. Frozen contract and V2/settings remain unchanged. No provider calls,
AWS recreation, deployment, private holdout inspection or new acceptance dataset.

## Session rules

Read this guide and the relevant contract; verify HEAD/status. Inspect only the
current deliverable. Preserve unrelated changes. V2 remains disabled. Do not
inspect private cases/labels, rerun consumed holdouts, recreate AWS, change flags,
call paid providers, deploy or publish without explicit scoped approval. No changes
to MCP, marketing, tenant UI, assistant or infrastructure repositories.

Follow inspect -> decide -> specify -> implement -> verify -> record. Important
tests must verify persisted and externally observed state. Record new ideas in
the deferred backlog; interrupt only for material safety/integrity/release issues.
After a frozen evaluation, outcomes are ship, narrow or stop. Repair/rerun requires
an exact reproduced safety violation and approval; poor scores or latency alone
do not qualify. Record every completed task's actual evidence and one next task.
