import os

from memoryos import Memory


def main() -> None:
    client = Memory(
        api_key=os.environ["MEMORYOS_API_KEY"],
        base_url=os.getenv("MEMORYOS_BASE_URL", "http://127.0.0.1:8000"),
    )
    memories = client.get(
        query="customer support style",
        external_user_id="student_44821",
        limit=4,
        format="xml",
        context_max_tokens=300,
    )
    xml_block = memories.system_prompt_addition if memories.has_context else "<memories />"
    print("Claude system prompt context:")
    print(xml_block)


if __name__ == "__main__":
    main()
