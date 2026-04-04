# Webhook Event Staging Verification

This runbook is for verifying tenant webhook delivery before production.

Use it in staging or pre-prod, not just local Docker, because webhook correctness depends on:

- real outbound network access
- DNS and TLS behavior
- timeout and retry behavior
- public endpoint reachability
- deployed Celery worker execution

## Goal

Verify that MemoryOS sends structured webhook events correctly and safely for:

- `quota.warning`
- `quota.critical`
- `quota.exhausted`
- `quota.reset`
- `mode.changed`
- `processing.delayed`
- `processing.recovered`

## What Must Be True First

- staging API is deployed
- staging Celery workers and beat are running
- staging database is migrated to the latest head
- tenant has:
  - `tenant_budgets.alert_webhook_url`
  - `tenant_budgets.webhook_secret`
- outbound HTTP from workers is allowed to the test webhook target

## Recommended Test Target

Use a request-capture endpoint that is safe for staging, for example:

- a temporary internal webhook receiver
- a staging-only RequestBin/Webhook.site style endpoint

Do not use a production customer endpoint for the first verification pass.

## Verification Checklist

### 1. Basic Delivery

Trigger one known event, for example `quota.warning`.

Verify:

- request arrives at the webhook target
- body is valid JSON
- headers are present:
  - `Content-Type: application/json`
  - `X-MemoryOS-Event`
  - `X-MemoryOS-Timestamp`
  - `X-MemoryOS-Signature`

### 2. Signature Verification

Using the tenant's stored `webhook_secret`, recompute:

- `HMAC-SHA256(secret, raw_request_body_bytes)`

Verify:

- recomputed hex digest matches `X-MemoryOS-Signature`

### 3. Retry Behavior

Configure the webhook target to return `500` or delay beyond `5s`.

Verify:

- MemoryOS retries up to 3 times
- retry spacing follows exponential backoff
- request path is not blocked
- final failure is logged without crashing the caller

### 4. Invalid URL Safety

Set `alert_webhook_url` to an invalid or blocked internal URL.

Verify:

- event is skipped safely
- caller does not fail
- warning is logged

### 5. Quota Warning And Critical Thresholds

Drive tenant usage across thresholds.

Verify:

- `quota.warning` fires when remaining budget crosses the configured threshold
- `quota.critical` fires when remaining budget crosses `5%`
- repeated checks do not spam duplicate events inside the stored threshold state

### 6. Mode Transition Events

Drive mode transitions:

- `FULL -> PASSTHROUGH`
- `FULL -> BLOCKED`
- `FULL -> DEGRADED_RETRIEVE`
- recovery back toward `FULL`

Verify:

- `mode.changed` fires with correct `from_mode`, `to_mode`, and `reason`
- `quota.exhausted` fires for passthrough/blocked transitions

### 7. Queue Delay And Recovery

Create queue depth beyond the delay threshold.

Verify:

- `processing.delayed` is sent
- same tenant/queue is not spammed more than once per 5 minutes
- when depth recovers, `processing.recovered` is sent

### 8. Monthly Reset

Trigger the reset task in staging.

Verify:

- counters are reset
- `quota.reset` is delivered
- payload includes `new_limit` and `reset_at`

## Recommended Commands

Apply schema:

```powershell
osenv\Scripts\python -m alembic upgrade head
```

Check schema head:

```powershell
osenv\Scripts\python -m alembic current
```

Run focused tests locally before staging:

```powershell
osenv\Scripts\python -m pytest tests\unit\test_webhook_event_service.py tests\unit\test_quota_manager.py tests\unit\test_quality_gate.py
```

## Pass Criteria

Treat webhook events as staging-verified only if all of the following are true:

- valid webhook deliveries succeed
- signatures verify correctly
- retries happen on failure
- invalid URLs are skipped safely
- quota and queue events fire with the correct payload shape
- no webhook send blocks the request path

## Still Better Verified After Deployment

After real deployment, verify:

- log visibility in the production logging stack
- real incident visibility if a webhook target is consistently failing
- real worker/network behavior under concurrent tenant load
