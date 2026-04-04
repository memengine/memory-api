# Terraform Local Values Checklist

Use this checklist when creating your local file:

- target file: `infrastructure/terraform/production.auto.tfvars`
- source template: `infrastructure/terraform/production.auto.tfvars.example`
- commit policy: never commit the real `production.auto.tfvars`

## Safe setup flow

1. Copy the example file locally

```powershell
Copy-Item infrastructure/terraform/production.auto.tfvars.example infrastructure/terraform/production.auto.tfvars
```

2. Open the new local file and replace every placeholder

3. Run:

```powershell
cd infrastructure/terraform
terraform init
terraform plan
```

## What each value should be

### Infrastructure identity
- `aws_region`
  - use: your AWS deployment region
  - current intended value: `ap-south-1`

- `project_name`
  - use: stable prefix for AWS resources
  - current intended value: `memoryos`

- `acm_certificate_arn`
  - get from: AWS ACM
  - use the certificate ARN for your production domain on the ALB

### App release bootstrapping
- `app_version`
  - local bootstrap value is fine at first
  - recommended initial value: `bootstrap`
  - later GitHub deploys will override runtime `APP_VERSION` with commit SHA

- `container_image_tag`
  - recommended initial value: `latest`
  - used for the first ECS bootstrap before GitHub deploy flow takes over

### Database
- `db_name`
  - recommended value: `memoryos`

- `db_username`
  - recommended value: `memoryos`

- `db_password`
  - create a strong random password
  - do not reuse local/dev password
  - store it in your password manager too

### Gemini
- not stored in `production.auto.tfvars`
- after Terraform apply, put it into local `.env` as `GEMINI_API_KEY`
- then push it with `scripts/push_secrets.sh`

### Qdrant
- `qdrant_api_key`
  - do not put in `production.auto.tfvars`
  - place it in local `.env` as `QDRANT_API_KEY`

- `qdrant_url`
  - do not put in `production.auto.tfvars`
  - place it in local `.env` as `QDRANT_URL`
  - get from: your Qdrant cloud cluster endpoint
  - should look like your hosted HTTPS endpoint, not local `http://localhost`

### Clerk
- `clerk_secret_key`
  - do not put in `production.auto.tfvars`
  - place it in local `.env` as `CLERK_SECRET_KEY`
  - get from: Clerk dashboard
  - backend secret key, not the publishable key

- `clerk_webhook_secret`
  - do not put in `production.auto.tfvars`
  - place it in local `.env` as `CLERK_WEBHOOK_SECRET`
  - get from: Clerk dashboard > Webhooks
  - should start with `whsec_`

- `clerk_jwt_issuer`
  - get from: Clerk instance settings / JWT issuer
  - should look like:
  - `https://your-instance.clerk.accounts.dev`
  - or your production Clerk issuer if using a custom domain setup

### Sentry
- `sentry_dsn`
  - do not put in `production.auto.tfvars`
  - place it in local `.env` as `SENTRY_DSN`
  - get from: Sentry project settings
  - use the real backend project DSN

### Database URL secret
- `DATABASE_URL`
  - do not handwrite it
  - after `terraform apply`, run:

```powershell
cd infrastructure/terraform
terraform output -raw database_connection_string
```

  - copy that value into local `.env` as `DATABASE_URL`
  - then run `scripts/push_secrets.sh`

## Recommended local file shape

```hcl
aws_region          = "ap-south-1"
project_name        = "memoryos"
acm_certificate_arn = "arn:aws:acm:ap-south-1:123456789012:certificate/replace-me"

app_version         = "bootstrap"
container_image_tag = "latest"

db_name             = "memoryos"
db_username         = "memoryos"
db_password         = "replace-with-strong-password"

clerk_jwt_issuer     = "https://your-instance.clerk.accounts.dev"
```

## Before you run apply

Check these manually:
- no placeholder value remains
- no local/dev secret is reused in production
- `acm_certificate_arn` is in the same region as the ALB
- the file is still untracked by git

Git check:

```powershell
git status --short
```

You should not see `production.auto.tfvars` staged for commit.

## After apply

Once Terraform is applied successfully:
- verify the ECR repo exists
- verify the ECS cluster and service exist
- verify the ALB DNS name is returned from Terraform outputs
- verify the RDS endpoint is returned from Terraform outputs
- copy `terraform output -raw database_connection_string` into local `.env` as `DATABASE_URL`
- populate local `.env` with:
  - `GEMINI_API_KEY`
  - `QDRANT_API_KEY`
  - `QDRANT_URL`
  - `CLERK_SECRET_KEY`
  - `CLERK_WEBHOOK_SECRET`
  - `SENTRY_DSN`
- run `bash scripts/push_secrets.sh`
- then run your GitHub deploy workflow to push the app image and update ECS

## Keep in mind

- Terraform is your infra bootstrap step
- GitHub Actions is your app deploy step
- after the first ECS bootstrap, app versioning should come from GitHub deploys, not manual edits




