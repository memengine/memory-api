# Approved display/operator implementation — 2026-10-12

Working source based on fa480dba62f843b43422c16e56c727f3a71767c1.
Owner subsequently approved a scoped tooling commit. Its Git commit records the
new source; no runtime/manifest repin, deployment or acceptance execution occurs.

Implemented optional loopback display around the existing request sender,
server-held capability/principal bindings, exact prior-step artifact references,
action/paint deadlines, preserved partial backend outcomes, and private automatic
unadjudicated artifacts/receipts. Offline human review verifies artifact hash and
run/case/source binding and creates a new receipt without altering originals.
Customer rendering is diagnostic until independently reviewed. Browser identity
receipts deliberately remain checked=false; no substitution qualification claimed.

Observer executable schema becomes explicit-operations-observer-3. Public schema
exports updated. Existing frozen v2 manifest/contract unchanged: the new observer
requires separate source/application/browser/runtime/manifest refreeze approval.

## Verification

Final authorized synthetic mechanism suite:

```powershell
$env:PYTEST_DISABLE_PLUGIN_AUTOLOAD='1'
.venv\Scripts\python.exe -B -m pytest tests/unit/test_explicit_operations_observer.py tests/unit/test_explicit_operations_operator.py -q -s -p no:cacheprovider --basetemp=.operator_checks_20261012_g
```

**38 passed in 2.19 s.** Existing observer checks plus 12 added mechanism checks.
ASGI/HTTP mock transports only; no backend, broker, SQL or provider contacted.
Checks cover sole request sender, exact response retention, five declared concurrent
attempts preserved, no request on action failure, no false successful paint receipt,
capability/origin gating, duplicate/extra-field/document rejection, source-step
binding, artifact tampering, explicit review and no overwrite of originals.
Synthetic temporary test data are not acceptance fixtures or independent cases.

Initial sandbox runners stalled or failed temporary-directory creation; stopped
only their known exec sessions. Authorized escalated runs with fresh workspace
temporary directories worked. Two initial adapter route tests exposed missing
Request annotations; corrected. A later concurrency test initially asserted a
single-response pointer against the existing attempts array; corrected the test
to assert five retained attempts. Final suite above passed after those changes.
These are development checks, not a consumed holdout or patch-and-rerun evaluation.

Scoped git diff --check passed. api has no working diff. Unrelated existing
changes preserved, nothing staged/committed. No private inputs/labels inspected.

## Working artifact SHA-256

| Artifact | SHA-256 |
| --- | --- |
| Observer | 48e5c07725af426c3c704f1e5ed9249513784ed20b246ffcfd88e055e0211666 |
| Display adapter | 5c66d24509b9f677878e8c8f23473d4c36718bde472b5fe503c3b1603d57e018 |
| Scenario schema | 4b0d266107a150f2c0f1e153c5cf1a4110ccadec75fc3acbaaa4b12558cf4585 |
| Manifest schema | 2089e27a07363286f02752f9dd54293be43c88c9788d3b60be362f165fc3a241 |
| Operator receipt schema | 934ea9249ad8b090df3014c1cb76ef621a73e04b3ab752f92453859addd88620 |

## Unresolved limits and next decision

No real browser paint or restart/reopened-session measurement performed. A
document ID change is a browser assertion, not authenticated customer identity.
Backend replies are displayed verbatim; this is not a customer SDK or proof of
correct customer rendering. Browser version is an attestation. Recorded paint
duration includes handoff/acknowledgement overhead. No queue collector, selective
dispatch fault control or fault coordination implemented. Those remain external
or unresolved; full P5/P6/P8/application qualification is not earned.

Commit approval covers tooling source only. Any later
real-browser mechanism check must be separately bounded and must not consume
acceptance data. No fixture creation, private authoring or evaluation authorized.
