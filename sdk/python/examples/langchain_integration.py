from memoryos import Memory


def main() -> None:
    memory = Memory(api_key="mem_live_xxx", base_url="http://127.0.0.1:8000")
    results = memory.get(
        query="programming language preferences",
        external_user_id="student_44821",
        limit=3,
    )
    context = results.system_prompt_addition if results.quota_mode != "PASSTHROUGH" else ""
    print("Inject this into your LangChain prompt:")
    print(context)
    memory.close()


if __name__ == "__main__":
    main()
