# Backend Pending Items

Date: 2026-03-26

This document lists the backend work that is still pending before the backend can be treated as fully production-verified.

## 1. Production Infrastructure Review And Apply

Status: pending manual review and apply

Items:
- Review Terraform in [infrastructure/terraform/main.tf](d:/memoryos/memory-api/infrastructure/terraform/main.tf)
- Review ECS/Fargate setup in [infrastructure/terraform/ecs.tf](d:/memoryos/memory-api/infrastructure/terraform/ecs.tf)
- Review RDS in [infrastructure/terraform/rds.tf](d:/memoryos/memory-api/infrastructure/terraform/rds.tf)
- Review Redis in [infrastructure/terraform/elasticache.tf](d:/memoryos/memory-api/infrastructure/terraform/elasticache.tf)
- Review ALB in [infrastructure/terraform/alb.tf](d:/memoryos/memory-api/infrastructure/terraform/alb.tf)
- Review ECR in [infrastructure/terraform/ecr.tf](d:/memoryos/memory-api/infrastructure/terraform/ecr.tf)
- Review Secrets Manager wiring in [infrastructure/terraform/secrets.tf](d:/memoryos/memory-api/infrastructure/terraform/secrets.tf)
- Run `terraform init`, `terraform plan`, and `terraform apply` manually

Blocked on:
- AWS account access
- real ACM certificate ARN
- real production values for secrets and networking decisions

## 2. GitHub Actions Secrets And Deployment Wiring

Status: workflow files created, not fully activated

Files:
- [ci.yml](d:/memoryos/memory-api/.github/workflows/ci.yml)
- [deploy.yml](d:/memoryos/memory-api/.github/workflows/deploy.yml)

Still needed:
- add GitHub secrets:
  - `AWS_ACCESS_KEY_ID`
  - `AWS_SECRET_ACCESS_KEY`
  - `AWS_ACCOUNT_ID`
  - `ECS_CLUSTER`
  - `ECS_SERVICE`
  - `PRODUCTION_HEALTHCHECK_URL`
  - `SLACK_WEBHOOK_URL`
  - `CODECOV_TOKEN`
- confirm Codecov integration
- run the workflows in GitHub and inspect the first successful/failed runs

## 3. Live Production API Verification

Status: partially verified locally, not fully verified in production

Still needed:
- deploy the API to production infrastructure
- verify:
  - auth flow with real Clerk JWTs
  - API key auth against the live DB
  - Celery worker and beat in production
  - Redis, Postgres, and Qdrant connectivity in production
  - ALB health checks
  - ECS scale-out behavior under load

## 4. Sentry Production Verification

Status: DSN wired in code, live event not yet verified

Files:
- [api/main.py](d:/memoryos/memory-api/api/main.py)
- [api/settings.py](d:/memoryos/memory-api/api/settings.py)

Still needed:
- restart API and Celery containers/services in the real target environment
- trigger a controlled test exception
- confirm event appears in Sentry
- optionally tune:
  - environment value
  - release value
  - sampling or tracing later if needed

## 5. SDK Live Verification

Status: local install and mocked behavior verified, live production checks pending

Reference:
- [sdk_verification_status.md](d:/memoryos/memory-api/docs/sdk_verification_status.md)

Still needed:
- Python quickstart against real MemoryOS API
- TypeScript quickstart against real MemoryOS API
- real `429` verification with a temporary low-rate-limit API key

## 6. Extraction Evaluation On Real Production-Like Usage

Status: spec evaluation passed target, broader live usage still pending

Files:
- [extraction_evaluation_report.md](d:/memoryos/memory-api/docs/extraction_evaluation_report.md)
- [extraction_evaluation_examples_1_19_20.md](d:/memoryos/memory-api/docs/extraction_evaluation_examples_1_19_20.md)

Still needed:
- validate extraction quality on real conversations
- inspect false positives and over-extraction in production traffic
- retune prompt if real-user conversations differ from the spec examples

## 7. Secrets Hygiene And Production Env Finalization

Status: local env exists, production secret source not finalized

Still needed:
- move production secrets fully into Secrets Manager / GitHub Secrets
- avoid using local `.env` as the source of truth for production
- confirm final values for:
  - `GEMINI_API_KEY`
  - `QDRANT_URL`
  - `QDRANT_API_KEY`
  - `CLERK_SECRET_KEY`
  - `CLERK_WEBHOOK_SECRET`
  - `SENTRY_DSN`
  - `DATABASE_URL`

## 8. Optional Cleanup / Hardening

Status: not blocking dashboard work

Possible follow-ups:
- add a dedicated app settings module for more env vars beyond Sentry
- add Terraform formatting/validation once Terraform is installed on the engineer machine
- add CI validation for Terraform
- review Celery persistence/beat schedule storage strategy for production
- decide whether LocalStack should remain part of normal dev Compose or become optional

## 9. Multi-Region / Residency Rollout Follow-Up

Status: Phase 1 code-ready, infrastructure rollout still pending

References:
- [region_assignment_runbook.md](d:/memoryos/memory-api/docs/region_assignment_runbook.md)
- [staging_preprod_verification_tracker.md](d:/memoryos/memory-api/docs/staging_preprod_verification_tracker.md)

Current state:
- the codebase is region-aware
- `regions` table exists
- `tenants.region_id` exists and defaults to `IN1`
- live infrastructure is still effectively `IN1` only

Still needed before real EU/US tenant assignment:
- deploy regional PostgreSQL, Redis, Qdrant, workers, and supporting infrastructure
- wire real regional secrets into AWS Secrets Manager
- verify request routing, cache invalidation, and regional connection selection in staging
- verify compliance boundaries for logs, backups, and model-provider traffic

Important operator rule:
- do not assign any tenant to `EU1` or `US1` yet
- tenant reassignment remains an operator-run process, not tenant self-service

## 10. API Deprecation Operations Verification

Status: versioning infrastructure is implemented and locally verified; staging ops checks still pending

References:
- [staging_preprod_verification_tracker.md](d:/memoryos/memory-api/docs/staging_preprod_verification_tracker.md)

Verified locally:
- unsupported API versions return a clear `400`
- deprecated field responses emit deprecation/sunset/link headers
- tenant deprecation usage is persisted and visible through `/v1/tenant/deprecation-usage`
- deprecation alert task can target tenants still using soon-to-sunset fields

Still needed:
- verify real tenant webhook delivery in staging
- verify structured deprecation usage logs in the deployed logging stack
- verify the warning flow across multiple replicas

## 11. Multi-Provider LLM Failover Verification

Status: abstraction implemented, Gemini live-verified, Cohere embedding live-verified, Anthropic and outage drills still pending

Current state:
- provider abstraction exists for:
  - Gemini
  - Anthropic
  - Cohere
  - local embeddings
- extraction path supports failover from Gemini to Anthropic in code
- embedding provider config supports Gemini, Cohere, and local
- Gemini live embedding and extraction were verified with the current real Gemini key
- Cohere live embedding was verified with the current real Cohere key
- router failover to Cohere was verified when the Gemini embed circuit was forced open

Still needed:
- obtain real Anthropic and Cohere credentials for staging or pre-prod
- verify real Anthropic extraction requests
- verify forced Gemini outage behavior in staging:
  - extraction falls back safely
  - retrieve serves cache-only for Gemini-backed collections
  - add does not mix Cohere vectors into Gemini collections

Current local note:
- Gemini and Cohere live provider calls are verified
- Anthropic still needs a real key before live extraction fallback can be proven

Important correctness rule:
- do not use Cohere to write embeddings into existing Gemini vector collections
- during Gemini embedding outage, prefer retry/degraded behavior over mixed-vector correctness bugs

## 12. Tenant Webhook Event Delivery Verification

Status: code path implemented and locally verified; real network delivery still pending

Reference:
- [webhook_event_staging_verification.md](d:/memoryos/memory-api/docs/webhook_event_staging_verification.md)

Verified locally:
- signed webhook payload generation
- retry behavior on failed webhook responses
- invalid URL skip behavior
- quota and queue systems dispatch structured event types

Still needed:
- verify real delivery to a reachable staging endpoint
- verify tenant-side signature validation against `X-MemoryOS-Signature`
- verify timeout/retry behavior against a slow or failing target
- verify deployed Celery workers and beat emit:
  - `quota.warning`
  - `quota.critical`
  - `quota.exhausted`
  - `quota.reset`
  - `mode.changed`
  - `processing.delayed`
  - `processing.recovered`

Why this matters:
- local tests prove payload shape and dispatch behavior
- only staging proves real outbound networking and delivery behavior

## 13. Public Status Page Before Launch

Status: intentionally deferred, but must be completed before real customer launch

Reference:
- [status_page_rollout_runbook.md](d:/memoryos/memory-api/docs/status_page_rollout_runbook.md)

Current decision:
- do not pay for or maintain the public status page during private development if it is not needed yet
- do not forget it before staging/external beta/production rollout

Still needed:
- create `status.memoryos.io` in Better Stack or Instatus
- add the four core components:
  - API
  - Memory Storage
  - Memory Retrieval
  - Vector Search
- wire monitors to:
  - `/health`
  - `/v1/internal/circuit-health` or equivalent internal integration
- connect CloudWatch/infrastructure alerts
- run one practice incident update flow
- link the status page from tenant-facing documentation and later from the dashboard

Why this matters:
- without a public status page, real incidents turn into avoidable support tickets
- this is part of the operational contract for a B2B infrastructure product

## Recommendation

The backend is far enough along to start dashboard work now.

The remaining backend tasks are mostly:
- production environment setup
- live verification
- AWS review/apply
- secret management finalization

That means dashboard implementation can proceed in parallel without waiting on every remaining backend production task.
## Deferred Warnings Cleanup

Do not do this immediately. Batch these into one cleanup pass before the first enterprise customer or before hiring the first engineer, whichever comes first.

- Upgrade `sentry-sdk`.
- Upgrade the Qdrant server to a version compatible with the current client.
- Add `.pytest_cache` to `.gitignore` and fix pytest cache permissions in CI/Docker.
- Audit async test teardown in `conftest.py` and adjacent test fixtures/helpers for the remaining coroutine warning.
