from memoryos import Memory


def main() -> None:
    memory = Memory(api_key="mem_live_xxx", base_url="http://127.0.0.1:8000")
    memories = memory.get("product preferences", external_user_id="student_44821", limit=5)
    prompt_addition = memories.system_prompt_addition if memories.quota_mode != "PASSTHROUGH" else ""
    print("Use this before your LLM chat request:")
    print(prompt_addition)
    memory.close()


if __name__ == "__main__":
    main()
