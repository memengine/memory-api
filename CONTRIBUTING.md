# Contributing to MemoryOS

Start with the [quickstart](README.md#quickstart),
[development guide](docs/DEVELOPMENT.md) and [Code of Conduct](CODE_OF_CONDUCT.md).
Documentation, setup reports and small synthetic regressions are welcome; no AWS
account, paid model key or private dataset is needed for the starter checks.

## First passing test run

In Bash with Git and Python 3.12+, run these five commands from the directory
where you want the clone. Dependency installation uses the package registry.
The tests use synthetic requests and require no provider key or services.

```bash
git clone https://github.com/memengine/memory-api.git
cd memory-api
python -m venv .venv
.venv/bin/python -m pip install -e '.[dev]'
.venv/bin/python -m pytest tests/unit/test_local_beta_package.py -q
```

For native Windows commands and broader checks, see the
[development guide](docs/DEVELOPMENT.md).

## Your first contribution

1. Choose an [open issue](https://github.com/memengine/memory-api/issues) or
   describe the problem in a new one. Small typo fixes can go straight to a PR.
2. Fork, clone and create a focused branch.
3. Follow [DEVELOPMENT.md](docs/DEVELOPMENT.md); start only services your change needs.
4. Make one coherent change and run the relevant checks.
5. Open a PR explaining the problem, resulting behavior, verification and limits.

Discuss larger API, permission, policy or schema changes before implementation.
An issue comment does not guarantee assignment. Maintainer review is required;
there is no promised response time.

## Starter contribution ideas

See the [good first issue drafts](.github/seed-issues/) for focused changes with acceptance criteria.

| Area | Useful work |
| --- | --- |
| Setup | Report OS/tool versions and the first failing command with sanitized output. |
| SDKs | Improve an error/example and verify it using mocked transports. |
| Governance | Add a focused regression for an existing scope, provenance or replay invariant. |
| Documentation | Clarify admission versus application, holds or operator review. |

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
[DEVELOPMENT.md#test-paths](docs/DEVELOPMENT.md#test-paths) and
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
