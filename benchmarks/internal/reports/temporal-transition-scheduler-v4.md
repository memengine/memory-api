# Temporal transition scheduler v4

## Isolated change

A dedicated Celery task now runs semantic-validity transitions every five minutes. Each
cycle enumerates active tenants, uses a separate transaction per tenant, processes all
overdue transitions (including those missed during downtime), and contains a tenant failure
without preventing other tenants from completing. The existing weekly lifecycle jobs were
not changed.

The task records and logs:

- scheduled interval and run timestamp
- tenant count, successful tenants and failed tenants
- activation and expiration counts
- per-tenant and total cycle duration
- failure tenant and exception type

It does not change extraction, conflict resolution, authority, ranking, validity semantics,
or the transition algorithm. Holdout was not accessed.

## Before/after lifecycle metrics

| Metric | Original baseline | Transition repair | Scheduled baseline |
|---|---:|---:|---:|
| Product-evaluable scenarios passed | 7/13 | 12/13 | 13/13 |
| Lifecycle success | 53.85% | 92.31% | 100% |
| Activation correctness | 0% | 100% | 100% |
| Expiration correctness | 0% | 100% | 100% |
| Claim-memory alignment | 50% | 100% | 100% |
| Outbox eventual consistency contract | 50% | 100% | 100% |
| Restart recovery | 50% | 50% | 100% |
| Timely activation/expiration | 0% | 0% | 100% |
| Retry safety | 66.67% | 100% | 100% |

All remaining frozen metric pass rates are 100%, including current/historical state,
premature/expired leakage, idempotency, single-winner correctness, PostgreSQL authority,
decay safety, cache validity and timezone correctness.

## Verification and measurements

- Frozen suite: 14 scenarios; 13 product-evaluable; 13 passed; zero product failures.
- The host-only frozen run still labels the database node a harness error because its
  subprocess does not inherit `.env`; the same PostgreSQL constraint test passed separately.
- Combined unit regressions: 67 passed in 17.23 seconds.
- PostgreSQL transition/concurrency and interval constraints: 2 passed in 3.14 seconds.
- Frozen harness duration: 122,804.60 ms total; 8,771.76 ms mean per scenario;
  822.55 ms minimum; 19,308.20 ms maximum. These values include pytest process startup and
  are not production transition latency.
- Operational scheduling bound: five minutes plus queue delay. Real cycle and per-tenant
  latency will be available from `temporal_transition_cycle_report` and
  `temporal_transition_report` logs once Celery beat/worker traffic executes naturally.

## Status

The frozen lifecycle/decay/temporal-activation phase is closed at 100% product-evaluable
correctness. No live task was manually triggered against existing tenant data during this
benchmark run.
