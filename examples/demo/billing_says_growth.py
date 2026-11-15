from scenario import extract, save, show, start


def main():
    start()
    record = extract("This account has subscription plan Growth.", "billing-001")
    save({"current": record})
    show("Current record", record)


if __name__ == "__main__":
    main()
