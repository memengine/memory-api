# Contributing to MemoryOS

Thanks for helping make governed context easier to use and reason about. Small
fixes, better explanations, synthetic tests and honest failure reports are all
valuable. You do not need a paid model key, cloud account or private dataset.

Read the [project status](README.md#project-status), [Code of Conduct](CODE_OF_CONDUCT.md)
and [local development guide](DEVELOPMENT.md) first. MemoryO is the product name;
existing MemoryOS package/import/API names are unchanged.

## Your first contribution

1. Find an [open issue](https://github.com/memengine/memory-api/issues), or open
   one describing a small problem and how you would verify a fix. For typos and
   similarly small documentation changes, a direct PR is fine.
2. Fork the repository and create a focused branch, for example
   `docs/windows-setup` or `test/scoped-preference-error`.
3. Follow [DEVELOPMENT.md](DEVELOPMENT.md). Start with the no-provider tests;
   start services only if the change needs them.
4. Make one coherent change, add the relevant checks, and open a PR describing
   what you ran and what remains unverified.

An issue comment is a request to coordinate, not an assignment guarantee.
Maintainer review is required before merge; no response-time SLA is promised.
Use the question template for help. Do not assume GitHub Discussions is enabled.

## Starter contribution ideas

These are proposed tasks, not claims that corresponding GitHub issues already
exist. Ask about current priority before investing substantial time.

| Idea | Useful completion evidence |
| --- | --- |
| Verify contributor setup on Windows or Linux | OS/tool versions, exact commands, first failure or successful checks; no credentials. |
| Improve a confusing SDK error/example | A small synthetic example and a mocked-transport contract test. |
| Add malformed scope/request coverage | A public-schema regression showing the expected refusal without model/provider calls. |
| Explain queued versus applied | Documentation that distinguishes admission, terminal status and correlated committed state. |
| Improve an existing test's diagnostics | Failure messages identify the invariant without leaking payloads or weakening assertions. |

Maintainers can turn agreed tasks into issues labelled `good first issue` or
`help wanted`. Labels and assignments are not created by these files.

## Scope and design discussion

The current focus is **explicit, application-declared preferences**, including
owner/scope bindings, attributable replacements, replay handling and usable SDK
contracts. Larger policy, schema, permission or architecture changes should be
discussed before implementation. Do not add a new UI, domain overlay, semantic
interpretation path or experimental rollout just to fix one example.

- PostgreSQL remains the durable authority; vectors/caches do not grant access.
- Tenant service keys are administrative within their tenant. Do not assume
  same-tenant per-human isolation on routes that do not accept an owner selector.
- The customer application supplies trusted identity and scope. Models cannot
  elevate authority, create permission, or stand in for authentication.
- Admission/completion alone does not prove the requested state was applied.
- Preserve provenance and revision chains except for explicitly supported
  privacy deletion. Do not introduce a permanent replay promise after deletion.
- Keep experimental flags off unless a separately approved change requires one.

## Tests and honest evidence

Run the smallest relevant tests first. [DEVELOPMENT.md](DEVELOPMENT.md#test-paths)
lists starter, SDK and service-dependent paths. CI's exact commands are in
[ci.yml](.github/workflows/ci.yml).

- Ordinary PR checks must not depend on personal provider keys, AWS access or
  private acceptance data. Paid provider evaluations stay manual and approved.
- Use disposable local resources and synthetic identities. Never point tests at
  production, shared experiments, or another contributor's database.
- Test changed request contracts, refusal paths and cross-tenant behavior where
  relevant. Add migration/downgrade coverage for schema changes.
- Preserve failures and unknowns. Explain whether a failure belongs to the
  product, fixture, tooling or environment; do not change a test's expected
  answer merely to obtain a green result.
- Passing unit tests, mocked SDK calls or an informed demonstration do not
  establish production readiness or independent acceptance.

**Do not inspect, publish, tune against or execute private holdouts/acceptance
cases.** They are not needed for a contribution. Existing evaluation freeze and
stop rules still apply; this guide does not authorize a qualification run.

## Pull request checklist

- [ ] The change has a clear problem, scope and verification method.
- [ ] Relevant tests/docs are included; exact commands and results are stated.
- [ ] Unrun checks and remaining limitations are disclosed.
- [ ] Public API/permission/state changes are explained, including migrations if needed.
- [ ] No credentials, personal data, private cases, generated state or unrelated edits are included.
- [ ] `git diff --check` passes for the changed files.

Prefer descriptive titles such as `fix: preserve SDK scope errors` or
`docs: clarify local setup`. Keep formatting-only sweeps separate. Review the
actual diff before staging, especially in a working tree with experiments;
avoid broad staging such as `git add .`.

AI-assisted contributions are welcome. Describe substantial assistance, review
the generated code yourself, and report actual verification rather than the
model's assertion that it works. Do not label AI-only review as independent
human adjudication.

## Security and licensing

Report vulnerabilities privately as described in [SECURITY.md](SECURITY.md).
Public issues should contain only sanitized synthetic reproductions. Do not
test systems you are not authorized to access.

Submit only work you have the right to share under the existing [MIT licence](LICENSE).
Identify external code/data sources and preserve their required notices; do not
add incompatible material or customer data.
