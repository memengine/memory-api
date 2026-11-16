# Cover the beta CLI's required preview arguments

Suggested label: `good first issue`

## Description

The local beta tests exercise paid-call opt-in and URL checks. They do not exercise the preview guard when the candidate or target is omitted.

## Files to touch

`beta/example.py`, `tests/unit/test_local_beta_package.py`

## Acceptance criteria

Add parameterized cases for a missing candidate and missing target. Assert the CLI exits with usage error before opening credentials or attempting HTTP.
