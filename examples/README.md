# MemoryOS examples

Run these commands from the repository root.

| Example | Requirements | Run |
| --- | --- | --- |
| Billing and CRM | Python 3.12+, Make | `make demo` |
| Billing and CRM without Make | Python 3.12+ | `python examples/demo/run.py` |
| LangGraph governed read | Python 3.12+, SDK and LangGraph | See the [LangGraph commands](langgraph/README.md) |
| API-backed Billing and CRM | Docker beta and an OpenAI key | Follow the [beta walkthrough](../beta/README.md#one-two-service-integration-example) |
| Python and TypeScript SDKs | A running MemoryOS server | Follow the [SDK quickstart](../docs/quickstart.md#2-save-and-read-a-memory) |

See the [no-key simulation](demo/README.md) for individual steps and the
[maintainer recording guide](recording/README.md) to regenerate the README GIF.
