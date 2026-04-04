# Status Page Rollout Runbook

This runbook explains how to set up the public MemoryOS status page and when to do each part.

## Recommendation

Do this before public launch.

You do not need to spend money on it while the product is still private and no external tenants depend on it yet.

For a B2B infrastructure product, a public status page is part of the platform trust layer. When something goes wrong, tenants should be able to check status first instead of opening a support ticket immediately.

Practical rule:

- private build / internal development:
  - can wait
- staging / external beta / enterprise pilot:
  - strongly recommended
- production launch for real tenants:
  - mandatory

## Recommended Tool

Use one of these:

- Better Stack
- Instatus

Both are acceptable for the current stage.

## Suggested Public URL

- `status.memoryos.io`

## What The Status Page Should Show

Keep it simple.

Show only:

- current component status
- active incidents
- incident history for the last 90 days

## Recommended Components

Create these components:

- API
- Memory Storage
- Memory Retrieval
- Vector Search

Optional later:

- Background Processing
- Tenant Webhooks

## What To Do Right Now

If you are still pre-launch and want to defer spend, treat this section as the setup checklist to run shortly before external rollout.

### 1. Create The Status Page

Create a Better Stack or Instatus account.

Create:

- `status.memoryos.io`

### 2. Add Core Components

Add:

- API
- Memory Storage
- Memory Retrieval
- Vector Search

### 3. Add Monitors Using Existing Endpoints

Use current backend endpoints first.

Recommended monitors:

- public health:
  - `GET /health`
- circuit/degradation visibility:
  - `GET /v1/internal/circuit-health`

If you use Better Stack uptime checks, point them at the deployed API URL, not localhost.

Example production-style targets:

- `https://api.memoryos.io/health`
- `https://api.memoryos.io/v1/internal/circuit-health`

Important:

- if `/v1/internal/circuit-health` is not intended to be internet-exposed, monitor it privately through your internal network or use CloudWatch-to-status-page integration instead

### 4. Connect Alerts

Connect status updates to:

- CloudWatch alarms
- ECS service alarms
- Redis / Postgres / Qdrant availability alerts
- queue backlog alerts if available later

### 5. Define Incident Templates

Prepare plain-English incident templates for:

- API degraded
- retrieval degraded
- vector search degraded
- provider outage
- scheduled maintenance

Each incident should say:

- what is affected
- who is affected
- what users should expect
- what you are doing

## What Backend Support Is Needed Right Now

Very little.

You do not need to build a special status-page backend before launching the public status page.

Existing endpoints are enough for Phase 1:

- `/health`
- `/v1/internal/circuit-health`

## What Can Wait Until After Dashboard

These are useful later, but not required now:

- showing status page data inside tenant dashboard
- incident banner inside the app
- backend endpoint that mirrors current status-page summary
- component-specific health aggregation endpoint
- internal admin UI for incident publishing

## What To Link Later

When ready, link the status page from:

- root README
- SDK READMEs
- tenant dashboard
- support documentation

## Suggested Rollout Phases

### Phase 1. Pre-Launch Setup

- create public status page
- add four core components
- add uptime monitors
- connect basic alerts

### Phase 2. Staging / Pre-Prod

- simulate one degraded event
- confirm incident publishing flow
- confirm component status can be updated quickly

### Phase 3. Dashboard Integration

- add a “Status” link in tenant dashboard
- optionally show a banner when there is an active incident

## Good Enough For Now

If you want the shortest correct path:

1. create `status.memoryos.io`
2. add:
   - API
   - Memory Storage
   - Memory Retrieval
   - Vector Search
3. monitor `/health`
4. connect alerts
5. link it later from dashboard

## Final Recommendation

- do not forget this before launching to real tenants
- if cost matters, delay the setup until you are close to staging, beta, or production rollout
- use current backend endpoints for monitoring
- do dashboard linking after the dashboard exists
- only build deeper backend status integration if later operational needs justify it
