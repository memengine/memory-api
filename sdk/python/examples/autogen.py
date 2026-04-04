from memoryos import Memory


def main() -> None:
    memory = Memory(api_key="mem_live_xxx", base_url="http://127.0.0.1:8000")
    search_results = memory.get("coding preferences", external_user_id="student_44821", limit=3)
    agent_context = [result.content for result in search_results.items]
    print("AutoGen agent memory context:")
    print(agent_context)
    memory.close()


if __name__ == "__main__":
    main()
