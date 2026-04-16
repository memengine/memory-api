# Tenant Dashboard Live Verification

This runbook is the step-by-step checklist for verifying the tenant dashboard against the live local backend before we move on to the operator dashboard.

## Goal

Verify that the tenant dashboard at `http://localhost:3000` is correctly connected to the MemoryOS API at `http://localhost:8000`, uses real data, and handles both success and failure states safely.

## Preconditions

Before starting, confirm all of the following:

1. Backend API is running and healthy.
   Expected:
   - `GET http://localhost:8000/health` returns `200`

2. Tenant dashboard dev server is running.
   Expected:
   - `http://localhost:3000` opens in the browser

3. Clerk auth is working in the tenant app.
   Expected:
   - sign-in succeeds
   - protected dashboard routes open after sign-in

4. Clerk organization is mapped to a real tenant in the backend.
   Expected:
   - the signed-in Clerk org maps to `tenants.clerk_org_id`

   Explanation:
   - each customer company in MemoryOS is a tenant
   - each tenant using the human dashboard should have one Clerk organization
   - that Clerk `org_id` is stored in `tenants.clerk_org_id`
   - when a tenant user signs into the dashboard under that Clerk org, the backend resolves the org to the correct MemoryOS tenant

   Example:
   - MemoryOS tenant: `Acme Learning`
   - MemoryOS tenant id: `uuid-123`
   - Clerk organization id: `org_abc123`
   - backend mapping:
     - `tenants.id = uuid-123`
     - `tenants.company_name = Acme Learning`
     - `tenants.clerk_org_id = org_abc123`

   Important:
   - Clerk organization is for dashboard users
   - MemoryOS API keys are still separate and are used by the tenant's app/backend systems
   - tenant API calls no longer fail with tenant-auth errors

5. CORS is enabled in the backend.
   Expected:
   - tenant dashboard requests from `http://localhost:3000` do not fail with browser CORS errors

6. Environment values are set correctly.
   Expected:
   - tenant app uses `NEXT_PUBLIC_API_BASE=http://localhost:8000`
   - tenant app uses `NEXT_PUBLIC_UPGRADE_URL=<current dev upgrade URL>`
   - backend uses `BILLING_UPGRADE_URL=<current dev upgrade URL>`

## Optional Step 0 - Fresh Local Reset And Reseed

Use this only if you want to remove old local test data and rebuild the dashboard data from scratch with your own tenant.

### Option A - Full local reset

This is the cleanest option for local development.

What it deletes:
- PostgreSQL data
- Redis data
- Qdrant data
- LocalStack state

Command:

```powershell
docker compose down -v
docker compose up -d --build
```

Expected:
- all containers start again
- database is empty except for schema after migrations run
- old queue state, old vectors, and old cache entries are gone

After startup, if migrations are not applied yet:

```powershell
osenv\Scripts\python -m alembic upgrade head
```

Expected:
- Alembic reaches `head`

### Option B - Keep containers, clear only database data

Use this only if you want to keep the containers alive and clear the records manually.

Important:
- this is more error-prone than Option A
- if you do this, also clear Redis and Qdrant data so you do not keep stale cache or vectors

If you choose this route, expect to clear:
- tenant rows
- proxy users
- memories
- api keys
- tenant budgets
- quality logs
- extraction jobs
- dead-letter state
- audit logs
- deprecation usage

Recommended rule:
- for local development, prefer Option A unless you have a specific reason not to

### After reset - rebuild the minimum required local data

For the tenant dashboard to work again, you need at least:

1. One tenant row
2. One tenant budget row
3. One tenant API key
4. One Clerk organization id mapped to that tenant
5. Some real memory activity if you want the charts and tables to be meaningful

### Step 0.1 - Create a fresh tenant and first API key

Command:

```powershell
osenv\Scripts\python scripts\create_tenant.py "My Local Tenant"
```

Expected:
- script prints the tenant id
- script prints a raw API key once
- a matching `tenant_budgets` row is created automatically

Important:
- save the printed API key immediately
- it will not be stored in plaintext

### Step 0.2 - Map your Clerk organization to that tenant

After tenant creation, copy your Clerk org id and store it on the tenant row.

SQL pattern:

```sql
UPDATE tenants
SET clerk_org_id = 'org_your_clerk_org_id'
WHERE id = '<your_new_tenant_uuid>';
```

Expected:
- the signed-in dashboard user under that Clerk org now resolves to the correct tenant

Minimum Clerk data you need:
- one Clerk organization id for the customer tenant
- users signed into the tenant dashboard under that organization

### Step 0.3 - Verify the mapping

SQL:

```sql
SELECT id, company_name, clerk_org_id
FROM tenants
WHERE id = '<your_new_tenant_uuid>';
```

Expected:
- `clerk_org_id` is populated with the correct `org_...` value

### Step 0.4 - Create useful dashboard data

The dashboard can technically open with only a tenant and API key, but the pages will be much more useful if you also create real activity.

Recommended minimum:
- at least 1 proxy user
- at least 5 to 10 successful `add()` calls
- at least a few days of activity if you want charts to look meaningful

How proxy users and memories are created:
- you do not need to insert proxy users manually for normal use
- call the real MemoryOS `add()` endpoint with your tenant API key
- MemoryOS will create proxy users, memories, jobs, and quality logs as needed

Expected:
- `/users` starts showing rows
- `/quality-log` starts showing activity
- `/` overview metrics stop looking empty

### Step 0.5 - Optional: seed larger local memory data

If you want more substantial local data for testing, use the existing seed script:

```powershell
osenv\Scripts\python scripts\seed_memories.py
```

Expected:
- benchmark-style data is inserted for local testing

Important:
- this is useful for load or retrieval testing
- it is not required for normal tenant dashboard verification

### Step 0.6 - Sign back in to the dashboard

After resetting and recreating the tenant:

1. restart the tenant dashboard dev server if needed
2. sign out
3. sign back in under the correct Clerk organization
4. reload `http://localhost:3000`

Expected:
- tenant dashboard resolves to the fresh tenant
- real tenant data loads from the new clean state

## Step 1 — Overview Screen

Open:
- `http://localhost:3000/`

Verify:
1. The page loads without redirect loops or blank sections.
   Expected:
   - sidebar is visible
   - overview heading is visible

2. The four metric cards load real data.
   Expected:
   - `Memories Stored` shows a number
   - `Quota Used %` shows a number or percentage
   - `Active Users (30d)` shows a number
   - `Gate Block Rate` shows a percentage or zero-state
   - no permanent `Failed to fetch` card when backend is healthy

3. Quota bar color is correct.
   Expected:
   - below 70%: green bar
   - 70% to 85%: amber bar
   - above 85%: red bar

4. Quota mode banners appear correctly.
   Expected:
   - if mode is `PASSTHROUGH`: red alert banner with upgrade CTA
   - if mode is `DEGRADED_RETRIEVE`: amber alert banner saying new memories are paused
   - if mode is `FULL`: no degradation banner

5. Memory Additions chart renders.
   Expected:
   - line chart appears
   - if data exists, at least the recent dates are shown
   - if no data exists, the UI shows a safe empty state instead of crashing

6. Gate Block Breakdown chart renders.
   Expected:
   - donut chart appears
   - legend shows visible layer labels when grouped data exists
   - empty data still renders safely

7. Recent Activity table loads.
   Expected:
   - rows show time, user, and status
   - status badge colors are sensible

## Step 2 — Users Screen

Open:
- `http://localhost:3000/users`

Verify:
1. User list loads.
   Expected:
   - rows appear from real API data
   - each row shows truncated user ID, memories, last active, quality score, status, actions

2. Search works with debounce.
   Expected:
   - typing does not fire instantly on every keypress
   - after a short pause, the table updates

3. Sort controls work.
   Expected:
   - changing sort updates the row ordering
   - supported sorts are `Last Active`, `Memory Count`, and `Quality Score`

4. Quality score badges are color-coded.
   Expected:
   - above `0.7`: green
   - `0.35` to `0.7`: amber
   - below `0.35`: red

5. Block User flow works.
   Expected:
   - clicking block opens a confirmation dialog
   - confirm text is clear and warns about stopping new memories
   - after confirm, the row refreshes via SWR invalidation

6. Load more works.
   Expected:
   - additional users append to the list instead of replacing it unexpectedly

7. Export CSV works.
   Expected:
   - clicking export downloads a CSV file
   - file contains current list data or server-provided CSV output

## Step 3 — User Detail Screen

Open:
- click a user from `/users`
- or open `http://localhost:3000/users/<ext_id>`

Verify:
1. Detail page loads only when navigated to.
   Expected:
   - no need to prefetch before visiting

2. Header shows the right summary.
   Expected:
   - truncated user ID
   - status badge
   - created date
   - last active date

3. Stat cards render.
   Expected:
   - `Memory Count`
   - `Avg Quality Score`
   - `Block History count`

4. Block history section is sensible.
   Expected:
   - blocked events show recent entries if they exist
   - layer and reason are readable

5. Memory list loads.
   Expected:
   - memory rows show content preview, category, importance, date
   - pagination or load-more behavior is usable

6. Delete memory works.
   Expected:
   - deleting a memory removes it from the list after refresh

7. GDPR Delete All flow is safely guarded.
   Expected:
   - red destructive action is shown clearly
   - typing `DELETE` is required
   - without exact `DELETE`, action stays blocked
   - after success, browser redirects back to `/users`

## Step 4 — Quality Log Screen

Open:
- `http://localhost:3000/quality-log`

Verify:
1. Summary bar loads.
   Expected:
   - shows total blocked today
   - shows layer breakdown
   - shows total calls and block rate

2. L2 advisory banner behaves correctly.
   Expected:
   - if L2 block rate is above 30%, amber advisory appears
   - banner can be dismissed

3. Filters work.
   Expected:
   - layer filter updates rows
   - date range filter updates rows

4. Table is readable.
   Expected:
   - columns include time, user, blocked at, reason, quality score
   - user links open the user detail page

5. Similarity field is conditionally shown.
   Expected:
   - L3 rows show similarity
   - non-L3 rows hide it cleanly

## Step 5 — API Keys Screen

Open:
- `http://localhost:3000/api-keys`

Verify:
1. Existing keys load.
   Expected:
   - key rows show name, permissions, created date, last used, revoke action

2. Create API Key dialog works.
   Expected:
   - name is required
   - permissions can be selected
   - submit creates a key successfully

3. Raw key reveal dialog is safely enforced.
   Expected:
   - raw key appears once in a separate dialog
   - copy button works
   - checkbox `I have copied this key` must be checked
   - close button is disabled until the checkbox is checked
   - after closing, the raw key is no longer shown

4. Revoke flow works.
   Expected:
   - revoke confirmation warns that API calls will stop immediately
   - after confirm, the key disappears or updates after refresh

5. Last Used formatting is sensible.
   Expected:
   - `Never` for unused keys
   - relative time for used keys
   - over 30 days old appears amber

Note:
- Older keys may show limited prefix detail if the backend does not expose a real prefix field for historical rows.

## Step 6 — Settings Screen

Open:
- `http://localhost:3000/settings`

Verify:
1. Settings page loads without crashing.
   Expected:
   - webhook form
   - overage policy controls
   - alert threshold control
   - plan card

2. Webhook test works.
   Expected:
   - clicking `Test Delivery` returns inline result
   - success example: `Delivered (200)`
   - failure example: `Failed — check URL`
   - timeout after about 6 seconds shows timeout message

3. Overage policy save works.
   Expected:
   - selecting a different policy and saving shows success feedback

4. Alert threshold control behaves safely.
   Expected:
   - slider moves correctly
   - save only submits changed fields

5. Plan card is correct.
   Expected:
   - current plan details are visible
   - upgrade CTA opens the current env-driven upgrade URL in a new tab

Note:
- If backend persistence for `alert_threshold_pct` is not yet wired, treat that slider as a dashboard-side control that still needs final backend persistence later.

## Step 7 — Sidebar And Navigation

Verify on desktop:
1. Active nav item is highlighted correctly on every page.
   Expected:
   - the current route is visibly highlighted

2. User session area is visible in the sidebar.
   Expected:
   - user identity / Clerk control is visible near the bottom

Verify on mobile:
3. Open the app under `768px` width.
   Expected:
   - sidebar collapses
   - hamburger opens and closes the menu correctly
   - navigation remains usable

## Step 8 — Failure-State Verification

Temporarily stop or disconnect the backend API, then reload the dashboard.

Verify:
1. Pages do not crash.
   Expected:
   - error cards appear instead of a React crash

2. Retry actions are visible.
   Expected:
   - sections with failed fetches show `Retry`

3. App chrome remains usable.
   Expected:
   - sidebar still renders
   - user can navigate between screens

After the check:
- start the backend again
- reload and confirm the data returns

## Step 9 — Final Sign-Off Criteria

The tenant dashboard is ready to hand off when all of the following are true:

1. All routes load without browser console auth/CORS errors.
2. No page depends on hardcoded mock data.
3. Success states use real API data.
4. Failure states show controlled UI, not crashes.
5. Destructive actions require explicit confirmation.
6. Upgrade CTA opens the current env-configured URL.
7. Clerk-authenticated tenant access consistently resolves to the correct backend tenant.

## Known Acceptable Limitations

These do not block dashboard verification if the rest is green:

1. Historical API key rows may not expose a true masked prefix if the backend does not provide it.
2. Alert threshold persistence may still need final backend support.
3. Some charts may show safe empty states if the tenant simply does not have enough live data yet.
