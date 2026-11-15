# Cover a timed-out beta job poll

Suggested label: `good first issue`

## Description

The beta test suite covers a failed job that eventually completes. It does not exercise the deadline or show that the helper never submits a replacement job after timing out.

## Files to touch

`tests/unit/test_local_beta_package.py`

## Acceptance criteria

Use a mocked monotonic clock and sleep. Assert a `TimeoutError`, only GET requests to the original job URL, and no real sleep.
