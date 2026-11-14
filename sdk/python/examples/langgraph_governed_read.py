"""Minimal LangGraph example: a governed MemoryOS read before the answer node.

The ``read_memory`` node reads MemoryOS for an account ID your application
supplies (from its authenticated session or database). Eligible records go to
the ``answer`` node. On a visibility hold, the graph returns an unavailable
reply and ends without invoking the answer node.

LangGraph is an application dependency here, not an SDK dependency:

    python -m pip install memoryo-sdk langgraph
    python sdk/python/examples/langgraph_governed_read.py

The demo runs offline with mocked SDK responses, a synthetic account ID and a
local answer stub. In your app, pass ``Memory(api_key=..., base_url=...)`` and
your model wrapper to ``build_graph``.
"""
from collections.abc import Callable
from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph
from memoryos import MemoryOSError, MemoryVisibility
from memoryos.types import MemoryResult, RetrieveResult

ACCESS_ERROR_REPLY = "I can't access your account details right now."
UNAVAILABLE_REPLY = "I can't confirm your account details yet. Please check back shortly."
NOT_FOUND_REPLY = "I couldn't find account details to answer that."


class ChatState(TypedDict, total=False):
    account_id: str
    question: str
    records: list[dict[str, Any]]
    reply: str


def build_graph(memory: Any, generate_reply: Callable[..., str]):
    """Compile a two-node graph. ``memory`` is a ``memoryos.Memory`` client."""

    def read_memory(state: ChatState) -> ChatState:
        try:
            context = memory.get(
                external_user_id=state["account_id"],
                query=state["question"],
                categories=["fact"],
                format="json",
            )
        except MemoryOSError:
            return {"reply": ACCESS_ERROR_REPLY}

        if context.memory_visibility is not None:
            return {"reply": UNAVAILABLE_REPLY}
        if not context.items:
            return {"reply": NOT_FOUND_REPLY}

        return {
            "records": [
                {
                    "id": record.id,
                    "content": record.content,
                    "source_event_id": record.source_event_id,
                }
                for record in context.items
            ]
        }

    def answer(state: ChatState) -> ChatState:
        return {"reply": generate_reply(state["question"], account_records=state["records"])}

    def route_after_read(state: ChatState) -> str:
        return END if "reply" in state else "answer"

    graph = StateGraph(ChatState)
    graph.add_node("read_memory", read_memory)
    graph.add_node("answer", answer)
    graph.add_edge(START, "read_memory")
    graph.add_conditional_edges("read_memory", route_after_read, ["answer", END])
    graph.add_edge("answer", END)
    return graph.compile()


class MockedMemory:
    """Stands in for ``memoryos.Memory`` and returns one canned SDK result."""

    def __init__(self, result: RetrieveResult) -> None:
        self.result = result
        self.calls: list[dict[str, Any]] = []

    def get(self, **kwargs: Any) -> RetrieveResult:
        self.calls.append(kwargs)
        return self.result


class AnswerStub:
    """Local stand-in for your model wrapper; it never calls a provider."""

    def __init__(self) -> None:
        self.calls = 0

    def __call__(self, question: str, *, account_records: list[dict[str, Any]]) -> str:
        self.calls += 1
        cited = ", ".join(record["id"] for record in account_records)
        return f"{account_records[0]['content']} [records: {cited}]"


def run_demo(name: str, result: RetrieveResult) -> None:
    account_id = "synthetic-account-0001"
    memory = MockedMemory(result)
    answer_stub = AnswerStub()
    graph = build_graph(memory, answer_stub)

    final = graph.invoke({"account_id": account_id, "question": "What plan is my account on?"})

    assert memory.calls[0]["external_user_id"] == account_id
    print(f"{name}: {final['reply']} (answer node calls: {answer_stub.calls})")
    if result.memory_visibility is not None:
        assert final["reply"] == UNAVAILABLE_REPLY and answer_stub.calls == 0
    else:
        assert answer_stub.calls == 1


def main() -> None:
    plan_record = MemoryResult(
        id="mem_synthetic_plan",
        content="The account is on the Growth plan.",
        category="fact",
        importance_score=5,
        relevance_score=1,
        context_snippet="The account is on the Growth plan.",
        source_event_id="billing-evt-synthetic-1",
    )
    current_record = RetrieveResult(items=[plan_record])
    # A hold stops the graph even when other items are still returned.
    visibility_hold = RetrieveResult(
        items=[plan_record],
        memory_visibility=MemoryVisibility(state="processing", scope="affected_context"),
        is_degraded=True,
    )

    run_demo("current record", current_record)
    run_demo("visibility hold", visibility_hold)


if __name__ == "__main__":
    main()
