# Dashboard Handoff Checklist

This file is the short checkpoint between backend platform work and dashboard work.

Use it when deciding:

- can we start dashboard implementation now?
- what still remains before real production launch?

## Dashboard Status

Yes, dashboard work can start now.

The backend has the major platform foundations in place:

- extraction job lifecycle, dead-letter flow, and watchdog
- queue fairness and tenant-aware queue routing
- circuit breakers and degraded-response signaling
- vector outbox pattern and reconciliation
- embedding model versioning and re-embedding support
- zero-downtime backfill framework and contract guards
- region-aware routing Phase 1
- API versioning and deprecation tracking
- structured tenant webhook event system
- SDK degradation signals

This means dashboard work does not need to wait for more backend feature implementation.

## What Still Remains Before Production

These are mostly staging, infrastructure, and operational verification tasks:

- review and apply AWS / Terraform infrastructure
- finalize production secrets in Secrets Manager
- verify GitHub Actions deployment wiring
- verify Sentry in a deployed environment
- run staging worker-crash and watchdog recovery drill
- run staging noisy-neighbor fairness drill
- run staging multi-replica circuit-breaker drill
- run staging tenant webhook delivery drill
- run staging deprecation webhook / logging drill
- run staging region-routing / Secrets Manager drill
- run staging LLM failover drill with all real provider credentials
- run SDK live verification against deployed API
- set up the public status page at `status.memoryos.io`
- review extraction quality on production-like traffic

## Source Of Truth

For the full detailed list, use:

- [staging_preprod_verification_tracker.md](d:/memoryos/memory-api/docs/staging_preprod_verification_tracker.md)
- [backend_pending_items.md](d:/memoryos/memory-api/docs/backend_pending_items.md)

## Practical Decision

- start dashboard now: yes
- launch to real tenants today: no
- finish staging / pre-prod verification before production: yes
