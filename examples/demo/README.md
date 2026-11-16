# No-key demo

Run `make demo` from the repository root with Python 3.12+ and Make. The simulation uses local fixture state and mock extraction and embeddings.

To run one step at a time, use a Bash shell. The Billing step resets the
synthetic state. CRM records a pending correction, then the agent reads with
visibility enforcement.

```bash
export MEMORYOS_DEMO_MOCK=1
python examples/demo/billing_says_growth.py
python examples/demo/crm_says_scale.py
python examples/demo/ask_agent.py
```
