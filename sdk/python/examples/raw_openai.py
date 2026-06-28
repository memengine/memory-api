import os

from memoryos import Memory


def main() -> None:
    memory = Memory(
        api_key=os.environ["MEMORYOS_API_KEY"],
        base_url=os.getenv("MEMORYOS_BASE_URL", "http://127.0.0.1:8000"),
    )

    external_user_id = "student_44821"
    memory.add(
        external_user_id=external_user_id,
        messages=[
            {"role": "user", "content": "I prefer concise answers with Python examples."},
            {"role": "assistant", "content": "Got it, I will keep examples Python-first."},
        ],
    )

    result = memory.get(
        query="how should I explain this coding concept?",
        external_user_id=external_user_id,
        limit=5,
        context_max_tokens=300,
    )
    prompt_addition = result.system_prompt_addition if result.has_context else ""

    print("Prepend this to your OpenAI system prompt:")
    print(prompt_addition)

    if result.retrieval_id:
        memory.feedback(
            retrieval_id=result.retrieval_id,
            outcome="used_successfully",
            used_memory_ids=[item.id for item in result.items],
            agent_confidence=0.9,
            metadata={"example": "raw_openai"},
        )


if __name__ == "__main__":
    main()
