# Test the demo opt-in before state writes

Suggested label: `good first issue`

## Description

The scripts enforce `MEMORYOS_DEMO_MOCK=1`, but the demo currently has no automated regression proving that an unset value leaves state untouched.

## Files to touch

`tests/unit/test_offline_demo.py`, `examples/demo/`

## Acceptance criteria

Use subprocesses in a temporary fixture directory, unset `MEMORYOS_DEMO_MOCK`, and assert a nonzero exit, the opt-in message, and no state file. The test must need no provider or containers.
