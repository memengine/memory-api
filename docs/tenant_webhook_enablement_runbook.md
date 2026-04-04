# Tenant Webhook Enablement Runbook

This runbook explains how webhook alerts are enabled for a tenant and who is responsible for each step.

## Purpose

MemoryOS can send tenant-specific webhook events such as:

- `quota.warning`
- `quota.critical`
- `quota.exhausted`
- `quota.reset`
- `mode.changed`
- `processing.delayed`
- `processing.recovered`

These events are sent to the tenant-specific URL stored in:

- `tenant_budgets.alert_webhook_url`

This is not an environment variable because each tenant can have a different webhook destination.

## Responsibility Split

### Done By The Tenant

The tenant is responsible for:

- providing their webhook destination URL
- sending that URL to our team if they are not using the tenant settings API directly
- implementing an endpoint that can accept HTTPS POST requests
- verifying the `X-MemoryOS-Signature` HMAC signature
- handling retries safely and idempotently on their side
- deciding whether to log, alert internally, or ignore non-critical events

### Done By The Operating Team

Our operating team is responsible for:

- enabling the tenant webhook URL in MemoryOS
- confirming the URL is valid and appropriate for staging/production use
- helping the tenant test delivery in staging
- verifying that Celery workers and beat are running
- investigating delivery failures if the tenant is not receiving events

### Done Internally By MemoryOS

MemoryOS handles these automatically:

- generating and storing `tenant_budgets.webhook_secret`
- signing webhook payloads with HMAC-SHA256
- retrying delivery up to 3 times
- skipping invalid or blocked webhook URLs safely
- firing the right event types when quota or queue transitions happen

## How To Enable A Tenant Webhook

There are two supported ways.

Important:

- a tenant sending us their webhook URL by email or chat does not enable anything by itself
- the URL must be stored in MemoryOS for that tenant before events will be sent
- after it is stored, MemoryOS sends events automatically

## Common Real-World Flow

### Path A. Tenant Sends URL To Our Team

This is the current common path when there is no tenant self-serve dashboard flow.

1. Tenant sends their webhook URL to our support/ops team.
2. Our team reviews it and saves it for that tenant.
3. Once saved, webhook delivery becomes automatic.

In this path:

- tenant provides the URL
- our team manually enables it in MemoryOS
- MemoryOS automatically handles sending after that

### Path B. Tenant Uses The Settings API

If the tenant is allowed to manage webhook settings through the API:

1. Tenant calls `PATCH /v1/tenant/settings`
2. MemoryOS stores the webhook URL for that tenant
3. Webhook delivery becomes automatic

In this path:

- tenant provides the URL directly through the product/API
- the enablement is handled internally by the API write
- MemoryOS automatically handles sending after that

### Option 1. Tenant/API Way

Use:

- `PATCH /v1/tenant/settings`

Example body:

```json
{
  "alert_webhook_url": "https://tenant.example.com/memoryos/webhook",
  "overage_policy": "warn"
}
```

This is the preferred self-service/operator-assisted path.

### Option 2. Operator SQL Way

Use SQL directly if needed:

```sql
UPDATE tenant_budgets
SET alert_webhook_url = 'https://tenant.example.com/memoryos/webhook'
WHERE tenant_id = '<tenant_uuid>';
```

## How To Verify It Is Enabled

Check the tenant settings endpoint:

- `GET /v1/tenant/settings`

Or inspect DB:

```sql
SELECT tenant_id, alert_webhook_url, webhook_secret
FROM tenant_budgets
WHERE tenant_id = '<tenant_uuid>';
```

Important:

- never share the `webhook_secret` value with anyone who should not verify the webhook
- never log the secret in plaintext

## How The Tenant Verifies The Signature

MemoryOS sends:

- `X-MemoryOS-Signature`

The tenant recomputes:

- `HMAC-SHA256(webhook_secret, raw_request_body_bytes)`

and compares it to the header value.

See examples in:

- [README.md](d:/memoryos/memory-api/sdk/python/README.md)
- [README.md](d:/memoryos/memory-api/sdk/typescript/README.md)

## Staging Verification Flow

1. Tenant gives a staging webhook URL.
2. Operating team saves it for the tenant.
3. Trigger a known event like `quota.warning`.
4. Tenant confirms:
   - request arrived
   - event body is correct
   - `X-MemoryOS-Signature` exists
   - signature verification succeeds

For the full checklist, use:

- [webhook_event_staging_verification.md](d:/memoryos/memory-api/docs/webhook_event_staging_verification.md)

## Important Rules

- Do not put tenant webhook URLs in `.env`
- Do not use one shared webhook URL for all tenants
- Do not enable a tenant webhook without a safe public HTTPS endpoint
- Do not treat webhook delivery as synchronous request-path behavior

## Short Summary

- Tenant provides the webhook URL and verifies signatures
- If the tenant only sends the URL by email/support, our operating team must enable it in MemoryOS
- If the tenant submits the URL through `PATCH /v1/tenant/settings`, it is enabled internally by that API flow
- Operating team helps test it and troubleshoot delivery
- MemoryOS handles signing, retries, and event dispatch automatically
