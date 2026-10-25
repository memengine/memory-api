# Contributor and maintainer workflows

## Active definitions

| Workflow | Trigger | Purpose |
| --- | --- | --- |
| `workflows/ci.yml` | Push/PR to `main` | Migrations, unit/governance integration tests, FAST regression, TypeScript SDK contracts |
| `workflows/benchmark-integration.yml` | Manual | Maintainer-run integration benchmark using disposable services |
| `workflows/benchmark-provider.yml` | Manual, explicit paid-call input and environment gate | Provider-backed development evaluation; not an ordinary PR requirement |

CI checks have not been removed or made non-blocking to improve appearance.
The integration benchmark's old weekly cron is removed so it does not repeatedly
run unattended while its setup/failure is unresolved. Removing that trigger is
not a fix, successful rerun, or reclassification of old failures.

## Archived AWS deployment definitions

`archived-workflows/` preserves the old image-build and production/staging ECS
deployment files outside GitHub Actions' active `.github/workflows/` directory.
The image-build file also pushes to ECR; it is not a credential-free local build.
The archive is historical, not a current deployment recipe or permission to
recreate infrastructure. Existing `.github/scripts/deploy-ecs.sh` is retained.

Before any future restoration, review current infrastructure, credential scope,
image destinations, migrations, environment approvals and costs deliberately.
Restoring a file is a separate deployment decision, not contributor setup.

These changes take effect for GitHub events using the relevant pushed/merged
revision. They do not disable workflows on historical branches, cancel runs
already queued/running, revoke credentials, or change repository/environment
settings. Maintainers must handle any such live actions separately. Old run
history may still appear in Actions; preserving it is intentional. Do not delete
failures simply to make the repository look green.
