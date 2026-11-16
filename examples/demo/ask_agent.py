from scenario import FALLBACK, QUESTION, load, retrieve, start


def main():
    start()
    print(f"Customer: {QUESTION}")
    items, visibility = retrieve(load(), QUESTION)
    if visibility:
        print(f"Memory visibility: {visibility['state']}")
        answer = FALLBACK
    elif items:
        answer = f"{items[0]['content']} [{items[0]['source_event_id']}]"
    else:
        answer = "I couldn't find your subscription plan."
    print(f"Agent: {answer}")


if __name__ == "__main__":
    main()
