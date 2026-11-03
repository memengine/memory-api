# Contributing to MemoryOS

Start with the [project status](README.md#project-status),
[development guide](DEVELOPMENT.md) and [Code of Conduct](CODE_OF_CONDUCT.md).
Documentation, setup reports and small synthetic regressions are welcome; no AWS
account, paid model key or private dataset is needed for the starter checks.

## Your first contribution

1. Choose an [open issue](https://github.com/memengine/memory-api/issues) or
   describe the problem in a new one. Small typo fixes can go straight to a PR.
2. Fork, clone and create a focused branch.
3. Follow [DEVELOPMENT.md](DEVELOPMENT.md); start only services your change needs.
4. Make one coherent change and run the relevant checks.
5. Open a PR explaining the problem, resulting behavior, verification and limits.

Discuss larger API, permission, policy or schema changes before implementation.
An issue comment does not guarantee assignment. Maintainer review is required;
there is no promised response time.

## Starter contribution ideas

| Area | Useful work |
| --- | --- |
| Setup | Report OS/tool versions and the first failing command with sanitized output. |
| SDKs | Improve an error/example and verify it using mocked transports. |
| Governance | Add a focused regression for an existing scope, provenance or replay invariant. |
| Documentation | Clarify admission versus application, holds or operator review. |

These are ideas, not assigned tickets or requests to add new product features.
Current delivery priorities are in [ROADMAP.md](ROADMAP.md).

## Engineering expectations

- PostgreSQL is the durable authority; vectors/caches do not grant permissions.
- Tenant-scoped keys enforce their permissions but do not authenticate individual
  people. The customer application owns user/project authorization.
- Preserve source, revision and audit relationships except for supported privacy
  deletion. Retained receipts do not provide replay protection after deletion.
- Check visibility before delivering context. Queued/completed status alone does
  not establish that the intended state became current.
- Keep experimental flags and provider-backed evaluations separate from normal
  contribution checks. Do not weaken authorization or assertions to pass a demo.
- Use only synthetic data and disposable resources. Never test against production
  or someone else's experiment. Private acceptance material is not contributor input.

Run focused checks appropriate to the change; see
[DEVELOPMENT.md#test-paths](DEVELOPMENT.md#test-paths) and
[CI](.github/workflows/ci.yml). Documentation changes need link/command review and
`git diff --check`, not a broad backend evaluation. Report unrun checks and the
first failure honestly. Mock checks do not prove production behavior.

## Pull requests

Keep the diff focused. Include relevant tests/docs and migration/compatibility
notes where needed. Exclude credentials, real conversations, private cases,
generated packages, caches and unrelated files. Review explicit paths before
staging; do not use broad staging in a dirty checkout.

AI assistance is welcome; review the output and report what actually ran.
Contributors remain responsible for correctness and external code/data licenses.
Submit only work you may share under the existing [MIT license](LICENSE).

Report vulnerabilities privately through [SECURITY.md](SECURITY.md).
