# Isolated staging deployment

Staging is a separate writable environment. It must never reuse production
PostgreSQL, Qdrant, or application-secret values.

## Before a staging plan

1. Create or obtain a staging DNS name and an ACM certificate in `ap-south-1`.
2. Provision separate staging PostgreSQL and Qdrant targets.
3. Copy `staging.tfvars.example` to ignored `staging.tfvars`, then replace the
   certificate and image-tag placeholders.
4. Copy `backend.staging.hcl.example` to ignored `backend.staging.hcl`, then
   replace the KMS-key placeholder with the state-bucket KMS ARN.
5. Confirm `10.31.0.0/16` does not overlap an existing VPC or connected network.

## State isolation

Switch the main Terraform directory to the empty staging state key. This does
not copy or alter production state:

```powershell
terraform init -reconfigure -backend-config backend.staging.hcl
terraform state list
```

An empty result is expected on the first staging initialization. Before working
on production again, reconfigure back to `backend.hcl` and verify its state.

## Image and secrets bootstrap

The first full ECS apply creates the staging ECR repository and Secrets Manager
records. To avoid starting an unhealthy task before its image and secret values
exist, make the first apply a zero-task bootstrap:

```powershell
terraform plan -var-file staging.tfvars -var 'ecs_api_desired_count=0' -var 'ecs_api_min_capacity=0' -var 'celery_scale_desired_count=0' -var 'celery_growth_desired_count=0' -var 'celery_starter_desired_count=0' -var 'celery_background_desired_count=0'
```

Review that plan separately, then apply it only when it has no destroys. It
creates the infrastructure but launches no API or worker tasks.

After that bootstrap:

1. Push the exact `container_image_tag` from `staging.tfvars` to the newly
   created staging ECR repository.
2. Put values into every `memoryos/staging/...` Secrets Manager record.
3. Confirm the PostgreSQL URL, Qdrant URL/key, and Clerk keys all belong to
   staging. Do not point a staging secret at a production target.
4. Run the normal `terraform plan -var-file staging.tfvars`; its desired counts
   return to one and it starts the API and workers.

Do not use a made-up image tag: ECS tasks will fail to start if the tag is
absent.

## Security rollout

Staging requires Redis TLS, Redis AUTH, and seven days of snapshots before any
customer-like traffic. The example configuration enables all three. Generate a
URL-safe token in the current PowerShell session immediately before each plan
or apply; it is intentionally not stored in `staging.tfvars`:

```powershell
$env:TF_VAR_redis_auth_token = python -c "import secrets; print(secrets.token_urlsafe(48))"
```

Do not paste the result into chat, a terminal transcript, a committed file, or
an issue. Terraform creates protected Secrets Manager records for the three
authenticated `rediss://` runtime URLs. ECS receives those URLs as secrets, and
Celery requires a verified TLS certificate.

The initial staging infrastructure plan still leaves application envelope
encryption disabled. First validate API, worker, PostgreSQL, Qdrant, Redis, and
outbox behavior. Only after that verification should KMS dual-write be enabled
in staging.
