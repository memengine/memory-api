# Selective fault tooling implementation — 2026-10-12

Source base caf361a96f556bd00ed04e94877533664b09cc4e; scoped tooling commit
authorized by the owner. Git records the resulting source commit.
Approved: runtime control/helper implementation and focused synthetic checks.
No live attachment/injection, memory request, provider call, fixture inventory,
private cases/labels, independent acceptance or deployment occurred.

Implemented one closed selector/controller module, wrapper factories for initial
and recovery publication and owned status read, strict isolated SQL/Redis readers,
bounded automatic pass-through restoration and unadjudicated hashed fault/queue
receipts. Existing api code, observer execution/schema exports, Northstar source,
runtime bootstraps/settings, governance and frozen thresholds remain unchanged.

Final command:

```powershell
$env:PYTEST_DISABLE_PLUGIN_AUTOLOAD='1'
.venv\Scripts\python.exe -B -m pytest tests/unit/test_explicit_fault_operator.py -q -s -p no:cacheprovider --basetemp=.fault_operator_checks_20261012_c
```

**21 passed in 1.25 s.** File-based synthetic controls/receipt artifacts with fake
publishers, async reads and SQL/Redis clients. No live service connections.
Earlier 19-check suite passed; adding queue receipts and corrupt-clock validation
exposed a test teardown error: the deliberately corrupted control was correctly
refused during context cleanup. Test now explicitly checks that refusal. Final
suite above passed; no failure was hidden or counted as an acceptance result.

See docs/EXPLICIT_FAULT_OPERATOR.md for attachment protocol and unsupported fault
classes. Wrappers were NOT attached to currently running local processes. Role
acknowledgements are control-plane observations, not health/recovery proof. Receipt
export requires actual selected refusal observations; owner_adjudicated remains
false. Automatic expiry invalidates a trial; it cannot manufacture a pass.

Backend unchanged; unrelated .gitignore, existing AGENTS/runtime notes, semantic
development edits and exploratory runner preserved. Only tooling, focused tests,
matching documentation and this checkpoint are authorized for the commit.
Next: separately approved bounded live control diagnostic. Live attachment,
runtime refreeze and acceptance execution remain separately gated.
