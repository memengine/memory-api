# Local display handoff — implementation checkpoint, 2026-10-12

Approved scope: display handoff, automatic rendering/identity observation and
offline human adjudication, with focused synthetic checks. No fault injection,
acceptance execution, independent case authoring or fixture creation.

The source is a working change, not a frozen candidate. Backend, governance,
V2 flags and the frozen acceptance contract are unchanged. The existing
observer-2 runtime manifest remains historical and untouched. Observer-3 must
receive separate source/runtime/application/manifest approval before any run.

## References invalidated by the tooling commit

The new Git commit records tooling source only; it does not refreeze the running
environment. These remain historical, not executable v3 approvals:

- `benchmarks/internal/protocols/explicit-operations-runtime-20261011.manifest.json`
  (SHA-256 7c0600b0b7218f2b4e4c46d4815a14500cfcacb5fabf953d8604afd0944c0f51):
  schema v2, source fa480db, old observer hash and old application/control client.
- `docs/EXPLICIT_OPERATIONS_PILOT_FREEZE_20261008.md`: its fa480db source and
  observer/runtime/schema pins do not identify the new tooling. Its acceptance
  criteria and stop rule remain binding, not replaced.
- `benchmarks/internal/reports/explicit-runtime-verification-20261011.md` and
  `explicit-runtime-acceptance-20261011.md`: evidence for the accepted old target,
  not attestation of a v3 display/browser integration or current process liveness.
- Metadata blueprint template/proposal references to fa480db, observer-2 and the
  old manifest: planning history, requiring reconciliation before later use.

Backend API tree/old runtime evidence remain supporting evidence because this
commit changes no backend source. The source HEAD equality check will nevertheless
reject the old manifest. No old manifest, runtime, frozen criteria or private data
is altered to conceal that difference.

## Request and display flow

The existing observer remains the only backend request sender. An optional
manifest `display` pins the adapter file SHA-256, loopback port, browser-version
attestation and server-side principal-environment-to-owner bindings. It contains
no credentials. The browser receives no API key, SQL access or backend transport.
It can start/acknowledge an already prepared operation using its opaque ID; it
cannot supply or replace its principal, owner, request payload or target. Unknown
fields, stale IDs and duplicate actions are refused. Trusted private scenario
payloads are preserved, including deliberate negative owner requests; the adapter
does not rewrite them or claim that their owner equals the displayed binding.

The observer exposes the resolved declaration/read context and actual response
to a loopback display via a run capability. The capability URL is private and
printed only during separately approved execution. Host/origin checks, no-store,
no-referrer and a restrictive CSP constrain the display. All values use textContent.
This is a local operator capability, not customer authentication or per-agent identity.

Foreground timing begins on the browser action and includes HTTP and display
handoff. HTTP elapsed time remains separately measured. The browser acknowledges
two animation frames only while visible. Recorded duration conservatively uses
the larger of browser elapsed time and server action-to-acknowledgement time.
This includes acknowledgement overhead and is diagnostic. Browser claims and
visibility/paint must still be independently reviewed; no real paint measurement
was demonstrated by the socket-free checks.

One display handoff wraps the existing HTTP step, preserving its declared attempt
count, concurrency and safe read polling. It does not add requests or retries.
Action/paint waits share the existing absolute deadline. A paint timeout retains
the received backend response and an incomplete private artifact; it cannot
create a success receipt. A fresh HTTP-client step also requires a different
browser document ID when a prior document exists for that principal. Document IDs
are browser assertions, not proof of reopened-session security.

Responses are shown verbatim with the warning that completion alone does not
prove application. No automatic applied label or semantic interpretation is added.
Queue acceptance is not application; precommit old committed state is permitted.

## Collection and human review

An external rendering/identity step may use `display_source` to identify the
exact earlier HTTP step whose artifact it consumes. This replaces `external_env`
for that observation only. No implicit last-response matching is used. Other
external receipts retain the existing local-file mechanism. A missing artifact
fails; no operator wait can reset a deadline.

Generated private `display-*` artifacts and `receipt-*` records are unadjudicated.
The observer summary reports pending adjudication and always pilot_qualified=false.
Identity receipts initially say browser_identity_checked=false: captured bindings
do not establish adversarial substitution or reopened identity. Human review must
assess those artifacts separately; merely confirming review does not flip that field.

After inspecting the actual artifact, the independent owner can create a new
reviewed rendering receipt offline with:

```powershell
.venv\Scripts\python.exe -B -m benchmarks.internal.explicit_operations_operator --receipt PRIVATE_RECEIPT --artifact PRIVATE_ARTIFACT --output NEW_PRIVATE_REVIEWED_RECEIPT --confirm-human-review
```

The command checks artifact hash and run/case/source binding, requires explicit
human confirmation and refuses an existing output. It never changes the original
artifact, original receipt, consumed marker or original evaluation results. No
backend/provider/evaluation call occurs. Its confirmation is an attestation, not
automatic verification of the human's judgment. Aggregate reconciliation after
review remains the owner's responsibility; no rerun is needed or authorized.

## Remaining boundaries

- Queue collection remains external; no new collector or queue mutation here.
- Fault injection/restoration/coordination remains unresolved and unimplemented.
- Real browser paint, reopening and identity-substitution qualification remain
  unverified. The adapter does not qualify a released SDK/customer application.
- Browser version is an attestation, not an automated browser inventory check.
- No fixtures, private cases, provider calls, AWS resources, runtime restarts,
  deployment, commit, candidate repin or acceptance execution occurred.

Verification evidence is in
`benchmarks/internal/reports/explicit-display-operator-20261012.md`.
