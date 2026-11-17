# LangGraph governed read

The first node reads MemoryOS for the account ID supplied by your application.
The graph calls the answer node only when the SDK returns usable records. A
visibility notice takes the fallback path even if the response includes items.

In a Bash shell with Python 3.12+ and an active virtual environment, run from
the repository root.

```bash
python -m pip install memoryo-sdk==0.1.2 langgraph
MEMORYOS_DEMO_MOCK=1 python examples/langgraph/agent.py
```

The two examples print the answer node call count. The current record reaches
the answer node once. The visibility hold returns a fallback with zero calls.
For an application, pass your server-side `Memory` client and model wrapper to
`build_graph`.
