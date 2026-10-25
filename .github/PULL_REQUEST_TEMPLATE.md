## Problem and scope

What user/developer problem does this change solve? Link an issue if applicable.
For a larger policy/API/schema change, link the prior design discussion.

## Changes

Describe the smallest coherent change. State whether API behavior, permissions,
experimental flags, state transitions or migrations changed.

## Verification actually performed

| Command/check | Result | Environment and limits |
| --- | --- | --- |
| | | |

List unrun checks, known failures and remaining unknowns. Distinguish model
claims/mock checks from actual program/service execution. For docs-only changes,
link/command review and a clean diff check are sufficient; do not claim tests ran.

## Safety and contribution checklist

- [ ] Reviewed [CONTRIBUTING.md](https://github.com/memengine/memory-api/blob/main/CONTRIBUTING.md) and kept the PR focused.
- [ ] Added appropriate synthetic tests/docs, or explained why they are not needed.
- [ ] Disclosed behavior/permission/schema changes and migration implications.
- [ ] No credentials, real conversations, dumps, private acceptance data or generated runtime state.
- [ ] Did not inspect/tune against a private holdout or relabel historical failures.
- [ ] No unapproved provider calls, deployment, experimental rollout or weakening of CI checks.
- [ ] Reviewed the staged diff and ran `git diff --check` on the changed files.

## Assistance and dependencies

Mention substantial AI assistance, external code/data and required licence
notices. The contributor remains responsible for reviewing and verifying the PR.
