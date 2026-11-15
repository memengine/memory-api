# MemoryOS examples

Run these commands from the repository root.

| Example | Requirements | Run |
| --- | --- | --- |
| Billing and CRM | Python 3.12+, Make | `make demo` |
| Billing and CRM without Make | Python 3.12+ | `python examples/demo/run.py` |
| LangGraph governed read | Python 3.12+, SDK and LangGraph | See the [LangGraph commands](langgraph/README.md) |
| API-backed Billing and CRM | Docker beta and an OpenAI key | Follow the [beta walkthrough](../beta/README.md#one-two-service-integration-example) |
| Python and TypeScript SDKs | A running MemoryOS server | Follow the [SDK quickstart](../docs/quickstart.md#2-save-and-read-a-memory) |

To run one step at a time, use a Bash shell. The Billing step resets the
synthetic state. CRM records a pending correction, then the agent reads with
visibility enforcement.

```bash
export MEMORYOS_DEMO_MOCK=1
python examples/demo/billing_says_growth.py
python examples/demo/crm_says_scale.py
python examples/demo/ask_agent.py
```

To regenerate the terminal recording, install [VHS and its dependencies](https://github.com/charmbracelet/vhs#installation), then run `make gif`.
