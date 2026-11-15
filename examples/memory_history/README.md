# Inspect a memory and its revision history

A small read-only example. It calls two endpoints and prints what they return:

```text
GET /v1/memories/{memory_id}
GET /v1/memories/{memory_id}/history
```

It uses only the Python standard library, does not import the SDK, and never
writes anything.

## Run without a server (mock transport)

The example ships a synthetic record in `fixtures/`, so you can see the output
before starting an API.

```bash
python examples/memory_history/inspect_memory_history.py --mock
python examples/memory_history/inspect_memory_history.py --mock --empty-history
python examples/memory_history/inspect_memory_history.py --mock --missing
```

## Run against a live API

Set the key in the environment (the script never prints it) and pass a memory id:

```bash
export MEMORYOS_API_KEY=...
export MEMORYOS_BASE_URL=http://127.0.0.1:18000   # optional, this is the default
python examples/memory_history/inspect_memory_history.py <memory_id>
```

PowerShell:

```powershell
$env:MEMORYOS_API_KEY = "..."
$env:MEMORYOS_BASE_URL = "http://127.0.0.1:18000"
python examples/memory_history/inspect_memory_history.py <memory_id>
```

## Output

```text
Memory mem_synthetic_0001
  current value : Customer is on the annual Team plan, renewing 2027-03-01.
  source        : event evt_20261104_0007, conversation conv_9f2c, agent agent_billing_bot, provenance {"event_id": "evt_20261104_0007", "service": "billing", "verified": true}
  updated at    : 2026-11-04T16:40:00Z
  category      : preference (importance 0.8, confidence 0.92)
  revisions     : 3
    v1  2026-10-02T09:15:00Z  created  reason=-
    v2  2026-10-28T11:03:00Z  manual_edit  reason=billing webhook: plan change
    v3  2026-11-04T16:40:00Z  conflict_update  reason=renewal date confirmed
```

A record with no revisions prints `revisions     : none recorded`. A record that
does not exist prints `Memory <id> was not found (MEM_404).` on stderr.

## Fields used

- Memory: `content` (the current value), `source_event_id`,
  `source_conversation_id`, `agent_id` and `provenance` (where it came from),
  plus `category`, `importance_score`, `confidence_score` and `updated_at`.
- History: `version_number`, `created_at`, `change_type` and `change_reason`.

## Exit codes

| Code | Meaning |
| --- | --- |
| 0 | Report printed |
| 1 | Missing memory id or `MEMORYOS_API_KEY` |
| 2 | Memory not found (`MEM_404`) |
| 3 | Any other API or connection error |

## Files

- `inspect_memory_history.py` — the example.
- `fixtures/memory.json`, `fixtures/history.json` — the synthetic record used by `--mock`.
