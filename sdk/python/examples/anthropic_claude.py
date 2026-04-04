from memoryos import Memory


def main() -> None:
    client = Memory(api_key="mem_live_xxx", base_url="http://127.0.0.1:8000")
    memories = client.get("customer support style", external_user_id="student_44821", limit=4)
    xml_block = memories.system_prompt_addition if memories.quota_mode != "PASSTHROUGH" else "<memories />"
    print("Claude system prompt context:")
    print(xml_block)
    client.close()


if __name__ == "__main__":
    main()
