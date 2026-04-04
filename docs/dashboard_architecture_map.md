# Dashboard Architecture Map

## Goal
Build the MemoryOS dashboard as the developer-facing control plane for:
- onboarding
- API key management
- memory browsing and editing
- usage visibility
- billing and plan upgrades
- account settings
- founder/admin oversight

## Important assumptions
- Vercel is optional hosting, not required architecture.
- Any old OpenAI mention should be treated as Gemini-based where an LLM or API key is referenced.
- The dashboard should consume the existing FastAPI backend, not introduce a second core backend.
- Clerk remains the auth layer for dashboard sign-in.

## Primary user areas

### 1. Auth entry
- Sign up / Login
- Provider: Clerk
- Purpose: developer onboarding and session creation

### 2. Developer dashboard
- Home / Overview
- API Keys
- Memories
- Usage
- Billing / Upgrade
- Settings

### 3. Founder/admin area
- Admin panel
- Used for internal operations, abuse review, plan changes, and user monitoring

## Page map

### Sign up / Login
- Audience: new developer
- Main jobs:
  - create account
  - sign in
  - establish dashboard session
- Dependencies:
  - Clerk frontend auth
  - Clerk webhook flow already present in backend

### Home / Overview
- Audience: logged-in developer
- Main content:
  - memory count
  - API calls this month
  - current plan
  - quick-start code snippet
- Main purpose:
  - orient the user fast
  - drive first successful integration

### API Keys
- Main actions:
  - create API key
  - list existing keys
  - revoke keys
- UX notes:
  - raw key shown once only
  - stored keys remain masked

### Memories
- Main actions:
  - list memories
  - search memories
  - filter by category
  - inspect memory detail
  - edit memory
  - delete or archive memory

### Usage
- Main content:
  - API call history
  - memory operation count
  - cost or plan usage this billing cycle
- UX notes:
  - strong candidate for charts, tables, and plan-limit warnings

### Billing / Upgrade
- Main actions:
  - view current plan
  - upgrade plan
  - manage payment method
- Integration notes:
  - Razorpay appears in the architecture as the payment provider

### Settings
- Main actions:
  - update email or account profile details
  - update memory preferences
  - delete account

### Admin panel
- Audience: founder/admin only
- Main actions:
  - list all users
  - inspect usage
  - flag abuse
  - change plans manually

## Shared layouts

### Developer layout
- Header
- Left navigation or top navigation
- Main content area
- Global user menu
- Global environment/status indicators if useful

### Admin layout
- Header
- Sidebar navigation
- Main content area
- Clear separation from normal user dashboard

## Shared components
- App shell
- auth guard
- page header
- stat card
- empty state
- loading skeleton
- error state
- table with filters
- cursor pagination controls
- search input
- category filter
- usage bar / quota indicator
- quick-start code snippet block
- API key reveal modal
- destructive action confirmation modal
- toast/alert system

## Data and backend mapping

### Auth and user session
- Clerk session for dashboard login
- Backend auth middleware already supports Clerk JWT
- Main backend routes:
  - `GET /v1/users/me`
  - `PATCH /v1/users/me/settings`
  - `GET /v1/users/me/export`
  - `DELETE /v1/users/me`

### Overview page data
- likely sources:
  - `GET /v1/users/me`
  - usage aggregation endpoint if added later
  - memory counts from user profile stats or memory list metadata

### API key management
- routes:
  - `GET /v1/api-keys`
  - `POST /v1/api-keys`
  - `DELETE /v1/api-keys/{id}`

### Memory management
- routes:
  - `GET /v1/memories`
  - `POST /v1/memories/retrieve`
  - `GET /v1/memories/{id}`
  - `PATCH /v1/memories/{id}`
  - `DELETE /v1/memories/{id}`
  - `GET /v1/memories/jobs/{job_id}`
- notes:
  - list view should use cursor pagination
  - retrieve endpoint can power semantic search or contextual memory search

### Billing and usage
- current architecture suggests plan and usage are part of the dashboard
- backend support may need additional endpoints later if current API is not enough

### Admin panel
- no dedicated admin API is defined yet
- treat admin as a later phase unless admin routes are added to backend

## State map

### Server state
- user profile
- API keys
- memories list
- memory detail
- retrieval results
- job status
- usage data
- billing/plan data

### Client state
- current filters
- current search query
- selected category
- current cursor
- modal visibility
- selected API key or memory item
- onboarding progress UI

## Suggested frontend architecture

### Framework
- Next.js App Router is a good fit for this architecture
- dashboard remains separate from API service

### Styling
- use the existing design system if one exists
- if not, define a consistent token layer first

### Data fetching
- prefer server-first routing where it helps auth and initial page load
- use client fetching for tables, filters, and dashboard interactions

### API client strategy
- reuse the TypeScript SDK where it helps
- for dashboard auth with Clerk JWT, a thin dashboard-specific fetch layer may be cleaner than forcing SDK-only usage everywhere

## Integration map

### Required
- Clerk
- MemoryOS FastAPI backend
- Redis/Postgres/Qdrant stay backend-side, not direct dashboard integrations

### Optional or secondary
- Vercel for hosting
- Razorpay for billing
- Resend for emails

### LLM-related assumption
- if any future dashboard AI helper exists, use Gemini assumptions, not OpenAI

## Suggested build phases

### Phase 1
- auth flow
- dashboard shell
- overview page
- API keys page

### Phase 2
- memories list
- memory detail
- edit and delete actions
- search and category filtering

### Phase 3
- usage page
- settings page
- export and account deletion flows

### Phase 4
- billing / upgrade
- admin panel
- founder tools

## Risks and gaps
- billing endpoints are not fully defined yet
- admin endpoints are not fully defined yet
- usage analytics shape may still need backend support
- memory preferences shape in settings may need refinement
- abuse workflows are product-defined but not fully backend-defined

## Working reference decisions for upcoming dashboard tasks
- Use this document as the dashboard build map.
- Treat Vercel as optional deployment guidance only.
- Treat Gemini as the LLM provider whenever older architecture references mention OpenAI.
- Build against the existing backend contract first; add backend changes only when the dashboard reveals a real gap.
