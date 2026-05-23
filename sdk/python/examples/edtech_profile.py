import os

from memoryos import Memory


def main() -> None:
    client = Memory(
        api_key=os.environ["MEMORYOS_API_KEY"],
        base_url=os.getenv("MEMORYOS_BASE_URL", "http://127.0.0.1:8000"),
    )

    profile = client.get_edtech_profile(external_user_id="student_44821")
    if profile is None:
        print("No EdTech profile yet. Add a student conversation first.")
        return

    print("Student:", profile.grade_level or "unknown grade")
    if profile.has_exam_context:
        print("Exam:", profile.exam_name, profile.exam_date)
    if profile.has_learning_profile:
        print("Learning style:", profile.explanation_style)

    print("Top weak topics:")
    for topic in profile.weak_topics[:3]:
        print("-", topic)


if __name__ == "__main__":
    main()
