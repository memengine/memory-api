---
name: Memory failure report
about: Share a reproducible stale, missing, or withheld memory in an agent
title: "[Memory failure] "
---

## What did the agent do?

Describe the wrong, missing, or withheld answer. Use a synthetic account and
remove credentials, private conversations, and customer identifiers.

## Which source should win?

List the synthetic source events in order, the expected current record, and
whether an operator should review the change.

## How can we reproduce it?

Include the MemoryOS commit or release, SDK version, and a minimal sequence of
writes and reads. Share the sanitized job status, `source_event_id`, and
`memory_visibility` fields if available.

## How does your app handle it today?

Describe the fallback, retry, or operator action your app uses.
