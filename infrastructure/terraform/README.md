# MemoryOS Terraform

This folder contains the AWS infrastructure for the MemoryOS API staging/production backend.

## What AWS manages

- VPC with public and private subnets
- Application Load Balancer for the FastAPI backend
- ECS Fargate services for the API and Celery workers
- ElastiCache Redis for Celery broker/result backend
- ECR repository for backend images
- Secrets Manager containers for runtime secrets
- KMS envelope-encryption key for governed memory content

## What is intentionally external

- PostgreSQL: Supabase-managed Postgres, provided to ECS through `DATABASE_URL`
- Vector database: Qdrant Cloud, provided through `QDRANT_URL` and `QDRANT_API_KEY`
- Frontend: Vercel apps point to the API domain, for example `https://api.memoryo.dev`

This keeps the expensive/stateful database operations on managed platforms while AWS runs the API, workers, Redis, load balancer, and deployment surface.

## Prerequisites

- Terraform `>= 1.5`
- AWS credentials configured locally or in CI
- ACM certificate ARN for the API domain
- Supabase Postgres connection string
- Qdrant Cloud cluster URL and API key
- OpenAI API key
- Clerk keys and webhook secret

## Setup

```bash
cd memory-api/infrastructure/terraform
cp production.auto.tfvars.example production.auto.tfvars
```

PowerShell:

```powershell
cd D:\memoryos\memory-api\infrastructure\terraform
Copy-Item production.auto.tfvars.example production.auto.tfvars
```

Edit `production.auto.tfvars` with non-secret deploy settings:

```hcl
aws_region          = "ap-south-1"
project_name        = "memoryos"
acm_certificate_arn = "arn:aws:acm:ap-south-1:123456789012:certificate/replace-me"

app_version         = "bootstrap"
container_image_tag = "latest"

redis_node_type = "cache.t4g.micro"
clerk_jwt_issuer = "https://your-clerk-instance.clerk.accounts.dev"
```

Before the first main-stack plan or apply, bootstrap encrypted remote state by
following [REMOTE_STATE.md](REMOTE_STATE.md). Do not commit `backend.hcl`, state
files, saved plans, or local `.tfvars` files.

For an isolated non-production deployment, follow
[STAGING_DEPLOYMENT.md](STAGING_DEPLOYMENT.md). Do not reuse production state,
secrets, databases, Qdrant collections, or certificate settings for staging.

Then run:

```bash
terraform init
terraform plan
terraform apply
```

## Push secret values

Terraform creates the Secrets Manager records. It does not store secret values in `.tfvars`.

```bash
aws secretsmanager put-secret-value --secret-id memoryos/DATABASE_URL --secret-string "postgresql+asyncpg://postgres:YOUR_PASSWORD@YOUR_SUPABASE_HOST:5432/postgres"
aws secretsmanager put-secret-value --secret-id memoryos/OPENAI_API_KEY --secret-string "sk-..."
aws secretsmanager put-secret-value --secret-id memoryos/QDRANT_URL --secret-string "https://YOUR_QDRANT_CLUSTER"
aws secretsmanager put-secret-value --secret-id memoryos/QDRANT_API_KEY --secret-string "..."
aws secretsmanager put-secret-value --secret-id memoryos/CLERK_SECRET_KEY --secret-string "..."
aws secretsmanager put-secret-value --secret-id memoryos/CLERK_WEBHOOK_SECRET --secret-string "..."
aws secretsmanager put-secret-value --secret-id memoryos/SENTRY_DSN --secret-string "..."
```

## Redis

You do not need to provide `REDIS_URL` manually. Terraform creates ElastiCache and injects:

- `REDIS_URL`
- `CELERY_BROKER_URL`
- `CELERY_RESULT_BACKEND`

into the ECS task definitions.

For a secure environment, enable Redis TLS, AUTH, and snapshot retention in its
environment-specific tfvars file. Supply the URL-safe AUTH token only in the
current shell, never in a file or Git:

```powershell
$env:TF_VAR_redis_auth_token = python -c "import secrets; print(secrets.token_urlsafe(48))"
```

With `redis_auth_enabled = true`, Terraform stores the authenticated
`rediss://` API/Celery URLs as Secrets Manager values and ECS injects them as
secrets. Celery uses certificate verification (`ssl_cert_reqs=required`). The
remote Terraform state is encrypted, but it is still sensitive operational
data: restrict access to the state bucket and do not print or commit plans.

## Memory content encryption

Terraform creates one customer-managed KMS key per Terraform environment and
grants the running API and Celery task role permission to request and decrypt
per-record data keys. The KMS key ARN is runtime configuration, not a secret;
no AWS access keys are injected into the containers.

Keep both settings disabled until the database migration has been deployed and
the staged rollout has been approved:

```hcl
data_encryption_provider   = "disabled"
data_encryption_write_mode = "disabled"
```

For a staging-only compatibility verification, use:

```hcl
data_encryption_provider   = "aws-kms"
data_encryption_write_mode = "dual-write"
```

Dual-write preserves existing plaintext columns while encrypted envelopes are
verified; it is not the final plaintext-removal state.

## Notes

- `production.auto.tfvars` is ignored by git through the repo `*.tfvars` rule.
- RDS is not created here because Supabase owns Postgres for this deployment plan.
- Qdrant is not created here because Qdrant Cloud owns vector storage.
- `APP_VERSION` should normally be set by CI from the Git SHA.
- Commit Terraform source (`*.tf`, `.terraform.lock.hcl`, examples and docs),
  but never commit state, local `.tfvars`, provider cache, or saved plans.
