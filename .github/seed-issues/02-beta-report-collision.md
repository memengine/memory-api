# Make a beta report filename collision easier to diagnose

Suggested label: `good first issue`

## Description

The beta CLI opens report files exclusively. Reusing `--output` can produce a filesystem traceback even though the CLI expects a fresh report filename.

## Files to touch

`beta/example.py`, `tests/unit/test_local_beta_package.py`

## Acceptance criteria

Pin the behavior with a synthetic existing file. Keep its bytes unchanged and report a concise error identifying `--output` as the argument to change.
