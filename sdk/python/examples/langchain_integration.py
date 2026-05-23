import os

from memoryos import Memory


def main() -> None:
    memory = Memory(
        api_key=os.environ["MEMORYOS_API_KEY"],
        base_url=os.getenv("MEMORYOS_BASE_URL", "http://127.0.0.1:8000"),
    )
    results = memory.get(
        query="programming language preferences",
        external_user_id="student_44821",
        limit=3,
        time_filter_days=30,
    )
    context = results.system_prompt_addition if results.has_context else ""
    print("Inject this into your LangChain prompt:")
    print(context)


if __name__ == "__main__":
    main()
