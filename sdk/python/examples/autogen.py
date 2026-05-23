import os

from memoryos import Memory


def main() -> None:
    memory = Memory(
        api_key=os.environ["MEMORYOS_API_KEY"],
        base_url=os.getenv("MEMORYOS_BASE_URL", "http://127.0.0.1:8000"),
    )
    search_results = memory.get(
        query="coding preferences",
        external_user_id="student_44821",
        limit=3,
    )
    agent_context = search_results.system_prompt_addition if search_results.has_context else ""
    print("AutoGen agent memory context:")
    print(agent_context)


if __name__ == "__main__":
    main()
