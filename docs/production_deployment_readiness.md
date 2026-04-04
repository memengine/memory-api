# Production Deployment Readiness

## What was improved

### 1. Versioning no longer needs manual source edits
- The API now accepts `APP_VERSION` as the primary runtime version value.
- `API_VERSION` is still accepted as a backward-compatible fallback.
- FastAPI app version, `/health` version, and Sentry release all use the same runtime version source.
- The deploy workflow now injects the short Git SHA into ECS as `APP_VERSION`.

### 2. Terraform secret coverage is aligned with the real app
- Added missing production secret support for:
  - `CLERK_WEBHOOK_SECRET`
  - `SENTRY_DSN`
- Added runtime config for:
  - `CLERK_JWT_ISSUER`
- ECS task definitions now expose:
  - `APP_ENV=production`
  - `APP_VERSION`
  - `CLERK_JWT_ISSUER`
- Terraform now creates empty Secrets Manager containers only.
- Secret values are no longer written through Terraform resources.
- Added [push_secrets.sh](d:/memoryos/memory-api/scripts/push_secrets.sh) for post-apply secret value upload.
- Added a sensitive Terraform output for the production `DATABASE_URL`.

### 3. ECS deployments now actually roll forward to the new image
- The previous deploy workflow only pushed a short-SHA image and forced a redeploy.
- ECS was still pinned to the old task definition image tag, so deploys could succeed without running the new build.
- The workflow now:
  - builds and pushes both `short-sha` and `latest`
  - fetches the active task definition
  - registers a new revision with the new image URI
  - injects the new `APP_VERSION`
  - updates the ECS service to the new task definition revision

### 4. Terraform value injection is now clearer
- Added [production.auto.tfvars.example](d:/memoryos/memory-api/infrastructure/terraform/production.auto.tfvars.example)
- Updated [README.md](d:/memoryos/memory-api/infrastructure/terraform/README.md) to explain:
  - local engineer flow with `production.auto.tfvars`
  - post-apply manual secret push flow
  - why real `.tfvars` files must stay out of git

### 5. Repo hygiene and Docker config were tightened
- Added more ignore rules for:
  - coverage files
  - local npm caches
  - Terraform plans
  - temporary local runtime folders
- Tightened `.dockerignore` so local env files, Terraform input files, caches, and reports do not get copied into image builds.
- Refactored `docker-compose.yml` so `api`, `celery-worker`, and `celery-beat` share the same build and env definition.
- Added `APP_ENV` and `APP_VERSION` defaults to local Docker Compose so container versioning stays aligned with the runtime config model.

## GitHub configuration you should create

### GitHub Secrets
- `AWS_ACCESS_KEY_ID`
- `AWS_SECRET_ACCESS_KEY`
- `AWS_ACCOUNT_ID`
- `ECS_CLUSTER`
- `ECS_SERVICE`
- `PRODUCTION_HEALTHCHECK_URL`
- `SLACK_WEBHOOK_URL`
- `CODECOV_TOKEN`

If you later decide to run Terraform from GitHub Actions too, only the Terraform-managed inputs belong there:
- `TF_VAR_db_password`
- `TF_VAR_clerk_jwt_issuer`
- `TF_VAR_acm_certificate_arn`

### GitHub Variables or environment-scoped values
These are not secrets, but they are still deployment config:
- `AWS_REGION`
- `ECR_REPOSITORY`
- `APP_ENV`

In the current workflow, `AWS_REGION` and `ECR_REPOSITORY` are already hardcoded to stable production defaults:
- `ap-south-1`
- `memoryos-api`

So you only need GitHub Variables if you want those to be editable without code changes.

## Local/manual Terraform apply options

### Option 1. Recommended for manual engineer apply
- copy `production.auto.tfvars.example` to `production.auto.tfvars`
- fill the Terraform-managed placeholders locally
- run `terraform init`
- run `terraform plan`
- run `terraform apply`
- copy the `database_connection_string` Terraform output into local `.env` as `DATABASE_URL`
- run [push_secrets.sh](d:/memoryos/memory-api/scripts/push_secrets.sh)

This is the recommended workflow for your current setup.

### Option 2. Optional future setup
- export only Terraform-managed `TF_VAR_*` env vars from GitHub Secrets
- run `terraform init`
- run `terraform plan`
- run `terraform apply`
- push secret values separately after apply

## Files updated in this pass
- [api/settings.py](d:/memoryos/memory-api/api/settings.py)
- [api/main.py](d:/memoryos/memory-api/api/main.py)
- [.env.example](d:/memoryos/memory-api/.env.example)
- [.github/workflows/ci.yml](d:/memoryos/memory-api/.github/workflows/ci.yml)
- [.github/workflows/deploy.yml](d:/memoryos/memory-api/.github/workflows/deploy.yml)
- [variables.tf](d:/memoryos/memory-api/infrastructure/terraform/variables.tf)
- [secrets.tf](d:/memoryos/memory-api/infrastructure/terraform/secrets.tf)
- [ecs.tf](d:/memoryos/memory-api/infrastructure/terraform/ecs.tf)
- [README.md](d:/memoryos/memory-api/infrastructure/terraform/README.md)
- [production.auto.tfvars.example](d:/memoryos/memory-api/infrastructure/terraform/production.auto.tfvars.example)
- [outputs.tf](d:/memoryos/memory-api/infrastructure/terraform/outputs.tf)
- [push_secrets.sh](d:/memoryos/memory-api/scripts/push_secrets.sh)
- [.gitignore](d:/memoryos/memory-api/.gitignore)
- [.dockerignore](d:/memoryos/memory-api/.dockerignore)
- [docker-compose.yml](d:/memoryos/memory-api/docker-compose.yml)

## Remaining production tasks outside code
- create the GitHub Secrets listed above
- fill local Terraform input values
- run `terraform plan` and `terraform apply`
- connect ECS/ALB/DNS in AWS
- do one live deploy from GitHub Actions
- verify `/health`, Sentry event capture, Clerk auth, Clerk webhook delivery, and ECS rollout behavior

## Files that should not go to GitHub
- `.env`
- any `.env.*` file except `.env.example`
- `production.auto.tfvars`
- any `*.tfvars`, `*.tfvars.json`, or `*.tfplan`
- local coverage outputs like `.coverage`, `coverage.xml`, and `htmlcov/`
- local npm caches such as `.npm-cache/`
- temporary local folders such as `edge-tmp/` and `tmp/`
- any manually prepared secret export file used before running `push_secrets.sh`
