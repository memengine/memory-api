# Give the offline demo a useful message for invalid local state

Suggested label: `good first issue`

## Description

The state loader handles a missing file. A truncated fixture file reaches `json.loads` and shows a traceback.

## Files to touch

`examples/demo/scenario.py`

## Acceptance criteria

Handle invalid JSON with a message to rerun the Billing step. Verify it with a disposable malformed state file, then rerun the three steps successfully.
