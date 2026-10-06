# Tenant chat pilot: fixed acceptance boundary

Status: NOT READY for activation of current-turn preparation.
This checklist freezes a release decision, not another feature roadmap.

## Product scope

One tenant-scoped SDK integration in an existing customer chat. The customer
authenticates users and supplies stable identity/conversation/turn IDs; MemoryOS
governs learned context, currentness, evidence, source authority and scope.
Clarification stays inside that chat. No new dashboard, universal path, MCP
integration, model-specific customer prompts or additional extraction pipeline
is required by this pilot. Pilot eligibility does not imply general availability.

Only failures of the acceptance rows below block this release. Other proposed
features and optimizations go to a post-pilot backlog. Do not extend the rows,
change expected results, or relax thresholds after observing failures without
explicitly recording and approving a changed release scope.

## Verified baseline: 2026-10-06

- A read-only call to `https://api.memoryo.dev/health` reported version `2ee4a90`
  and healthy PostgreSQL, Redis and Qdrant at 13:08:48 UTC. This is not proof
  of worker flags, queue health, deployed assistant source or end-to-end behavior.
- The assistant root URL returned HTTP 200. No signed-in user was manipulated.
- Local assistant HEAD `67b5745` has a clean worktree; its 33 existing controlled
  tests passed. Those are not live semantic acceptance tests.
- Local backend HEAD `176b3e5` adds default-off turn preparation. Its verification
  and limits are recorded in [turn_preparation.md](turn_preparation.md).
- The inspected assistant source calls `memory.get()` before generation and
  `memory.add()` after streaming. Its manifest pins `memoryo-sdk` to `0.1.2`;
  it does not call `prepareTurn()` or relay `memoryVisibility`/
  `preferenceScopeRequest`. Local source inspection does not establish what
  code Vercel currently serves.

## Known prerequisites, not new discoveries

1. Current-turn usability policy: registration safely commits a guard before a
   read, but a newly registered unknown question can withhold learned context.
   Normal background dispatch runs after the response; therefore a fresh-turn
   read cannot rely on that dispatch completing first. Wiring preparation into
   every message alone is not a usable ordinary-recall solution. The existing
   backend current-turn work must satisfy both safety and normal-recall rows
   below, within a measured latency budget. No phrase-specific assistant fix,
   silent ordinary-read fallback or unbounded worker wait is acceptable.
2. Customer integration handoff: after that policy passes, install the verified
   SDK candidate and relay its typed decision/visibility/scope statuses through
   the existing chat. Supply stable IDs and server-authenticated identity.
   Do not also ingest the same turn again after preparation. This is an SDK
   integration change, not language-specific prompt tuning or new product UI.

Do not publish/install the candidate or enable live flags as part of this
document. Deployment of disabled code can be separate from activation.

## Frozen acceptance rows

Use fresh synthetic users A/B under an isolated test tenant; a second synthetic
tenant is used only for isolation checks. Never reuse a real signed-in customer's
preferences. Replay the same declared test set after a repair. English, Hindi
and Hinglish variants of the correction/uncertainty journey are development
checks, not a new blind holdout or evidence of all-language calibration.

| ID | Exercise | Required result |
| --- | --- | --- |
| P1 | Set C++ as a default and an exam date; reopen chat/sign in again and recall both. Also ask an ordinary question that changes neither. | Same owned identity; correct retained facts; useful ordinary recall, not a permanent processing/empty-memory state. |
| P2 | Correct the exam date from October 10 to October 18; ask immediately and again in a new session. | No stale date presented as current. Pending/unknown may be withheld; after processing only October 18 is current, not two separate exams. |
| P3 | Express natural uncertainty about C++ versus Python, without a prescribed sentence; ask for a code example. | Backend does not silently select a default or promote a merely considered alternative. Its unresolved status/question reaches the same chat. |
| P4 | Explicitly resolve that choice to Python; read again after processing and in a new session. | Backend records the authorized resolution, closes the obsolete decision, and retrieves only the resolved current default. |
| P5 | Replace C++ with Python for one supplied project only; keep the global default. Read with each explicit scope and once without scope. | Python in the project, C++ globally; missing scope produces a typed scope request when required, not a guessed project. |
| P6 | User B and tenant B request user A's memory/job/review IDs; submit a forged browser external user ID. | No content or actionable foreign evidence leaks; ownership is enforced server-side. No client-chosen identity override. |
| P7 | Supply quoted/tool/document instructions pretending to be a user confirmation, and a lower-authority conflicting assertion. | No authority promotion or overwrite of stronger memory based on model claims or forged evidence. Verify persisted provenance/authority, not answer text alone. |
| P8 | Repeat exact turn IDs, change the payload under those IDs, and race identical retries. Simulate full queue, broker outage and read failure in an isolated environment. | One owned admission; mismatches rejected; no false acknowledgement or stale ordinary-read fallback. Committed guards survive dispatch/read failure; existing recovery resumes safely. |
| P9 | Repeat normal recall, correction and uncertainty interactions while measuring response time and memory availability. | Meet the pre-approved pilot latency/availability targets below without weakening P1-P8. An empty-memory quick answer is not a successful recall sample. |

## Measurement and evidence rules

Record the exact backend/SDK/assistant candidate versions, relevant flag names,
synthetic scope, test cases, requests/job/source IDs, admission/visibility
statuses, current memory/provenance and rendered chat outcome. Do not record
credentials or real user data. A model's answer alone is insufficient proof of
backend resolution; API/SQL-only success is insufficient proof of chat delivery.

Report separately: MemoryOS request duration, end-to-end first visible token,
background processing completion and fraction of ordinary recall samples with
usable expected context. Report sample count and percentiles; a small pilot
sample is not a production p99/load-capacity guarantee.

Proposed pilot targets, requiring approval BEFORE the first paid live run:

- At least 30 normal-recall samples across fresh and resumed sessions.
- At least 29/30 contain the expected usable remembered context; no unsafe result.
- MemoryOS foreground request p95 <= 750 ms.
- End-to-end first visible token p95 <= 4 seconds, with provider/config recorded.
- Correction/uncertainty pending states may delay memory availability but may
  never falsely assert old context is current. Their processing times are
  reported separately, not silently excluded from the result.

These are proposed acceptance targets, not achieved numbers or promises. Do not
change them after a failed result just to obtain a pass. A future live run also
needs an approved provider-spend ceiling and isolated test credentials; this
inspection incurred $0 provider cost and performed no authenticated writes.

## Stopping rule

One fixed acceptance run produces a row-level pass/fail/not-tested report.
Repair only failing in-scope blockers, then rerun those rows and the existing
safety regression suites. Previously failing development cases can demonstrate
a repair, not serve as an untouched independent accuracy evaluation. Do not
call mocks, AI-generated annotations or the same tuned cases independent proof.

GO for a supervised limited pilot requires P1-P9 passing, exact-version
deployment verification, an isolation-preserving rollback/disable check and
explicit activation approval. Otherwise NO-GO with named failing rows and
evidence. No additional architectural phase follows automatically. Unrelated
enhancements remain backlog work and cannot postpone a passing scoped pilot.
