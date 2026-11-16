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

## README recording

With Docker Compose running, use `make gif` from the repository root. It starts a
disposable beta, runs the five numbered scripts against its API, records them
with VHS, and removes the temporary stack. A local model fixture supplies
extraction and embeddings. Storage, review, visibility, history, and audit
fields come from MemoryOS responses.
