# Exercise the LangGraph missing-memory and SDK-error replies

Suggested label: `good first issue`

## Description

The runnable example covers a record and a visibility hold. The graph also has distinct fallback branches for an empty response and a `MemoryOSError`.

## Files to touch

`examples/langgraph/agent.py`, `examples/langgraph/README.md`

## Acceptance criteria

Add two mock invocations that print each fallback and assert the answer node is skipped in both. Keep the demo offline.
