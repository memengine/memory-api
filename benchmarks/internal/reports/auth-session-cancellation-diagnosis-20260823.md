# Authentication session cancellation diagnosis — 2026-08-23

Status: diagnosis complete; cancellation hypothesis rejected. Production behavior unchanged.

## Method

- Dedicated disposable stack and deterministic provider mode.
- No memory writes and zero provider calls/cost.
- Holdout excluded.
- Benchmark-only `AsyncSession` lifecycle markers recorded enter, exit start, exit completion, transaction state, owner, age, and cancellation status.
- 100 concurrent authenticated job-read requests used a 500 ms client deadline after clearing only the disposable auth cache.
- PostgreSQL was sampled every 500 ms for 120 seconds.

An initial 50 ms attempt expired before reaching the API and was classified as a harness timing miss; it was not used for product conclusions.

## Results

- Client outcomes: 100/100 deliberate timeouts.
- Authentication markers confirmed all requests reached `_authenticate_api_key`.
- Session lifecycle events: 297 entered, 297 exit-started, 297 exit-completed.
- Authentication-owned sessions: 100.
- Entered sessions missing an exit: zero.
- Cancelled exits: zero.
- Sessions with a transaction at exit start: 198.
- Sessions still reporting a transaction after exit completion: zero.
- Maximum session scope age: 40.126 seconds.

Client disconnection did not cancel the server authentication tasks. The server continued processing, and `AsyncSession.__aexit__` completed for every measured session. `BaseHTTPMiddleware` cancellation is therefore not the source of the retained transactions.

## Remaining PostgreSQL inconsistency

- PostgreSQL peak sessions: 55.
- Peak idle-in-transaction: 23.
- Observer failures: zero.
- Final state after the observation window: 22 idle and one idle-in-transaction session.
- The remaining backend was global async engine `mosb:7:a:2`, waiting on `ClientRead`, with query `BEGIN;` and transaction age 30.293 seconds.

This backend remained idle-in-transaction even though every instrumented ORM session reached exit completion and reported `in_transaction=false`. The confirmed boundary is now below request/session cleanup: connection pool return/reset, asyncpg transaction state, or pool pre-ping/reset interaction.

## Decision and next isolated investigation

Do not add cancellation shielding or modify `BaseHTTPMiddleware` based on this evidence.

Next, instrument the async pool checkout/checkin/reset/invalidate lifecycle with the underlying PostgreSQL backend PID and driver transaction status. Determine whether the retained `BEGIN` backend is:

1. checked into the pool without a server rollback;
2. opened by `pool_pre_ping` after session cleanup;
3. detached from the session during circuit-breaker timeout/error handling; or
4. associated with a separate engine sharing the same application label.

No pool reset, pre-ping, circuit-breaker, or transaction behavior should change until that boundary is confirmed.
