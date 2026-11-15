from scenario import extract, load, save, show, start


def main():
    start()
    state = load()
    correction = extract("Correction: This account has subscription plan Scale.", "crm-001")
    state["pending"] = correction
    save(state)
    show("Current record", state["current"])
    show("Correction flagged for review", correction)


if __name__ == "__main__":
    main()

